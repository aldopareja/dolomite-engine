import logging
from typing import List, Tuple, Union

import torch
from transformers import AutoConfig, AutoTokenizer
from transformers.integrations import HfDeepSpeedConfig

from ..arguments import ExportArgs, InferenceArgs, TrainingArgs
from ..distributed import get_deepspeed_config
from ..enums import (
    AttentionImplementation,
    DistributedBackend,
    GradientCheckpointingMethod,
    LossMask,
    Mode,
    PaddingSide,
)
from ..hf_models import is_padding_free_transformer_supported
from ..hf_models.modeling_utils import is_glu
from ..utils import log_rank_0, register_profiler, register_timer, warn_rank_0


class ModelWrapper(torch.nn.Module):
    """Model class which wraps any HuggingFace model"""

    @register_profiler("initialize_model")
    @register_timer("initialize_model")
    def __init__(self, args: Union[TrainingArgs, InferenceArgs, ExportArgs], mode: Mode):
        """initializes a Model wrapper for a HuggingFace model

        Args:
            args (Union[TrainingArgs, InferenceArgs, ExportArgs]): arguments based on training / inference mode
            mode (Mode): training / inference mode for running the program
        """

        super().__init__()

        self.mode = mode
        self.model_name = args.model_args.model_name
        self.model_class = args.model_args.model_class
        self.gradient_checkpointing_method = args.distributed_args.gradient_checkpointing_method
        self.gradient_checkpointing_args = args.distributed_args.gradient_checkpointing_args

        self._setup_input_device()

        self.distributed_backend = None
        self.stage = None
        if self.mode == Mode.training:
            self.distributed_backend = args.distributed_args.distributed_backend
            self.stage = args.distributed_args.stage

        if self.model_name is None:
            self.config = AutoConfig.for_model(**args.model_args.pretrained_config)
        else:
            self.config = AutoConfig.from_pretrained(
                self.model_name, trust_remote_code=args.model_args.trust_remote_code
            )
        log_rank_0(logging.INFO, self.config)

        self.attention_implementation = args.model_args.attention_implementation
        self.use_padding_free_transformer = args.model_args.use_padding_free_transformer
        if self.use_padding_free_transformer:
            assert is_padding_free_transformer_supported(
                self.model_class, self.config.model_type
            ), "padding free transformer is not supported with the specified model"

            assert (
                self.attention_implementation == AttentionImplementation.flash_attention_2
            ), "padding free transformer only works with flash attention"

        self.is_encoder_decoder = self.config.is_encoder_decoder
        self.tuning_method = args.tuning_args.tuning_method
        self.dtype = args.model_args.dtype

        tokenizer_name = args.tokenizer_args.tokenizer_name
        if tokenizer_name is None:
            tokenizer_name = self.model_name
        assert tokenizer_name is not None, "pass a tokenizer"
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
        self.padding_side = PaddingSide(
            self.tokenizer.padding_side
            if args.tokenizer_args.padding_side is None
            else args.tokenizer_args.padding_side
        )

        self._setup_model(args)

        self.loss_mask = None
        if self.mode == Mode.training:
            self.loss_mask = args.training_parameters.loss_mask
            if self.is_encoder_decoder:
                assert (
                    self.loss_mask == LossMask.output_only
                ), "only output_only loss mask is supported with encoder decoder models"

        if self.mode == Mode.training:
            neft_alpha = args.research_args.neft_alpha
            if neft_alpha is not None and neft_alpha > 0:
                self._override_embedding_forward_with_neft_forward(neft_alpha)

        additional_special_tokens = args.tokenizer_args.additional_special_tokens
        if additional_special_tokens is not None and len(additional_special_tokens) > 0:
            original_vocab_size = len(self.tokenizer)

            self.tokenizer.add_special_tokens({"additional_special_tokens": additional_special_tokens})
            log_rank_0(logging.INFO, f"added {len(additional_special_tokens)} tokens")

            if len(self.tokenizer) != original_vocab_size:
                self.model.resize_token_embeddings(len(self.tokenizer))

    def _setup_model(self, args: Union[TrainingArgs, InferenceArgs, ExportArgs]) -> None:
        if self.model_name is None:
            model_kwargs = {"config": self.config}
        else:
            model_kwargs = {
                "pretrained_model_name_or_path": self.model_name,
                "trust_remote_code": args.model_args.trust_remote_code,
            }

        model_kwargs["use_cache"] = self.mode == Mode.inference
        if self.attention_implementation is not None:
            model_kwargs["attn_implementation"] = self.attention_implementation.value
        if self.use_padding_free_transformer:
            model_kwargs["use_padding_free_transformer"] = True

        def _get_model(**extras):
            if self.model_name is None:
                model = args.model_args.model_class.from_config(**model_kwargs, **extras)
            else:
                model = args.model_args.model_class.from_pretrained(**model_kwargs, **extras)

            return model

        if self.mode == Mode.training:
            # this tells from_pretrained to instantiate directly on gpus
            # this only instantiates a single instance of the model across the ranks
            if self.distributed_backend == DistributedBackend.deepspeed:
                if args.model_args.efficient_cpu_initialization:
                    self.deepspeed_config = HfDeepSpeedConfig(get_deepspeed_config(args))

                self.model = _get_model()
            elif self.distributed_backend == DistributedBackend.torch:
                # DDP
                if self.stage == 0:
                    with torch.device(self.input_device):
                        self.model = _get_model()
                # FSDP
                else:
                    if args.model_args.efficient_cpu_initialization:
                        assert self.model_name is None

                        with torch.device("meta"):
                            self.model = _get_model()
                        self.model = self.model.to_empty(device=self.input_device)
                    else:
                        self.model = _get_model(device_map=self.input_device)
        else:
            self.model = _get_model(torch_dtype=self.dtype)

    @register_profiler("generate")
    @register_timer("generate")
    def generate(self, batch: Tuple[List[int]], generate_kwargs: dict) -> List[str]:
        """generate function for a batch

        Args:
            batch (dict): a dict of key, value pairs for a batch
            generate_kwargs (dict): generate kwargs for the batch

        Returns:
            List[str]: list of generated text. input is trimmed from the generated text
        """

        if self.use_padding_free_transformer:
            raise NotImplementedError("padding free transformer doesn't support generation")

        batch = self.prepare_batch(batch)

        for i in batch:
            batch[i] = batch[i].to(self.input_device)

        generated = self.model.generate(**batch, **generate_kwargs, eos_token_id=self.tokenizer.eos_token_id)

        if not self.is_encoder_decoder:
            generated = generated[:, batch["input_ids"].shape[1] :]

        # add 1 since eos token to also count eos in generated tokens
        num_generated_tokens = ((generated != self.tokenizer.eos_token_id).sum(dim=-1) + 1).tolist()
        generated_text = self.tokenizer.batch_decode(generated, skip_special_tokens=True)

        return generated_text, num_generated_tokens

    def get_model_tflops(self, batch_size: int, sequence_length: int) -> None:
        b = batch_size
        s = sequence_length
        h = self.config.n_embd
        f = self.config.n_inner
        n = self.config.n_head
        k = self.config.num_key_value_heads
        l = self.config.n_layer
        v = self.config.vocab_size

        mlp_flops = 4 * b * s * h * f
        if is_glu(self.config.activation_function):
            mlp_flops += 2 * b * s * h * f

        attention_flops = 4 * b * s * h * (h * (1 + k / n) + s)

        forward_flops = attention_flops + mlp_flops

        backward_flops = 2 * forward_flops
        if self.gradient_checkpointing_method == GradientCheckpointingMethod.block:
            backward_flops = forward_flops / self.gradient_checkpointing_args.get("checkpoint_every", 1)

        model_flops = l * (forward_flops + backward_flops)
        model_flops += 6 * b * s * h * v
        model_flops /= 10**12

        return model_flops

    @register_profiler("prepare_batch")
    @register_timer("prepare_batch")
    def prepare_batch(self, batch: Tuple[List[int]]) -> dict:
        """prepares the batch with padding to pass into the forward function of the HuggingFace model

        Args:
            batch (Tuple[List[int]]): input tokens and output tokens. Output tokens are optional when running generation but required for training.

        Returns:
            dict: dict containing input_ids, attention_mask and labels if outputs is specified
        """

        result = {}

        if self.mode == Mode.training:
            inputs, outputs = batch
            assert outputs is not None, "outputs can't be None during training"
        else:
            inputs = batch
            outputs = None

        input_ids, attention_mask, labels = _pad(
            inputs=inputs,
            outputs=outputs,
            pad_token_id=self.tokenizer.eos_token_id,
            padding_side=self.padding_side,
            is_encoder_decoder=self.is_encoder_decoder,
            loss_mask=self.loss_mask,
            use_padding_free_transformer=self.use_padding_free_transformer,
        )

        result["input_ids"] = input_ids
        result["attention_mask"] = attention_mask
        if self.mode == Mode.training:
            result["labels"] = labels

        return result

    def reset_parameters(self) -> None:
        if hasattr(self.model, "reset_parameters"):
            self.model.reset_parameters()

    def _override_embedding_forward_with_neft_forward(self, neft_alpha: float):
        if not hasattr(self.model, "get_input_embeddings"):
            raise Exception(
                "`get_input_embeddings` is not implemented for this model so its not possible to inject noise to input"
                " embeddings. Please implement `get_input_embeddings` ot set `neft_alpha` to None"
            )

        original_forward = self.model.get_input_embeddings().forward

        def _noisy_forward(x: torch.Tensor):
            x = original_forward(x)

            # to check if we are in eval mode we use self.training instead of self.model.training
            if self.training:
                mag_norm = neft_alpha / torch.sqrt(torch.tensor(torch.numel(x)))
                return x + torch.zeros_like(x).uniform_(-mag_norm, mag_norm)

            return x

        # overrides the forward function of torch.nn.Embedding
        self.model.get_input_embeddings().forward = _noisy_forward

    def _setup_input_device(self) -> None:
        if self.mode == Mode.training:
            # if using deepspeed
            self.input_device = torch.cuda.current_device()
        else:
            self.input_device = 0
            if not torch.cuda.is_available():
                warn_rank_0("no CUDA device found, running on CPU")
                self.input_device = "cpu"

    def save_pretrained(self, save_path: str) -> None:
        self.tokenizer.save_pretrained(save_path)
        self.model.save_pretrained(save_path)


def _pad(
    inputs: list,
    outputs: list,
    pad_token_id: int,
    padding_side: PaddingSide,
    is_encoder_decoder: bool,
    loss_mask: LossMask,
    use_padding_free_transformer: bool = False,
    labels_mask_value: int = -100,
) -> Tuple[List[int], List[int]]:
    """pads the arrays with the specified padding value

    Args:
        inputs (list): input token ids
        outputs (list): output token labels
        pad_token_id (int): token id to pad with
        padding_side (PaddingSide): padding side for the tensors
        is_encoder_decoder (bool): whether the model is an encoder-decoder or a decoder-only model
        loss_mask (LossMask): masking methodology for loss
        use_padding_free_transformer (bool): whether to use padding free transformer
        labels_mask_value (int): mask value to use for labels

    Returns:
        Tuple[List[int], List[int]]: token ids and the corresponding attention masks
    """

    # labels is None when outputs is None
    labels = None

    if is_encoder_decoder:
        input_max_length = max(list(map(len, inputs)))

        if padding_side == PaddingSide.left:
            input_ids = [[pad_token_id] * (input_max_length - len(array)) + array for array in inputs]
            attention_mask = [[0] * (input_max_length - len(array)) + [1] * len(array) for array in inputs]
        else:
            input_ids = [array + [pad_token_id] * (input_max_length - len(array)) for array in inputs]
            attention_mask = [[1] * len(array) + [0] * (input_max_length - len(array)) for array in inputs]

        if outputs is not None:
            assert (
                loss_mask == LossMask.output_only
            ), "only output_only loss mask is supported with encoder decoder models"

            output_max_length = max(list(map(len, outputs)))
            # right padding for labels
            labels = [array + [labels_mask_value] * (output_max_length - len(array)) for array in outputs]
    else:
        if use_padding_free_transformer:
            input_ids = inputs
            attention_mask = None

            if loss_mask == LossMask.output_only:
                labels = [
                    [labels_mask_value] * (len(array_in) - len(array_out)) + array_out
                    for array_in, array_out in zip(inputs, outputs)
                ]
            elif loss_mask == LossMask.no_mask:
                labels = inputs
            else:
                raise ValueError(f"unexpected loss_mask ({loss_mask})")
        else:
            max_length = max(list(map(len, inputs)))

            if padding_side == PaddingSide.left:
                input_ids = [[pad_token_id] * (max_length - len(array)) + array for array in inputs]
                attention_mask = [[0] * (max_length - len(array)) + [1] * len(array) for array in inputs]

                if outputs is not None:
                    if loss_mask == LossMask.output_only:
                        labels = [[labels_mask_value] * (max_length - len(array)) + array for array in outputs]
                    elif loss_mask == LossMask.no_mask:
                        labels = inputs
                    else:
                        raise ValueError(f"unexpected loss_mask ({loss_mask})")
            else:
                input_ids = [array + [pad_token_id] * (max_length - len(array)) for array in inputs]
                attention_mask = [[1] * len(array) + [0] * (max_length - len(array)) for array in inputs]

                if outputs is not None:
                    if loss_mask == LossMask.output_only:
                        labels = [
                            [labels_mask_value] * (len(array_in) - len(array_out))
                            + array_out
                            + [labels_mask_value] * (max_length - len(array_in))
                            for array_in, array_out in zip(inputs, outputs)
                        ]
                    elif loss_mask == LossMask.no_mask:
                        labels = inputs
                    else:
                        raise ValueError(f"unexpected loss_mask ({loss_mask})")

    if not use_padding_free_transformer:
        input_ids = torch.tensor(input_ids)
        attention_mask = torch.tensor(attention_mask)
        if labels is not None:
            labels = torch.tensor(labels)

    return input_ids, attention_mask, labels