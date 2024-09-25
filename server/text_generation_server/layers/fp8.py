import torch

from dataclasses import dataclass
from typing import Optional, Tuple, Union, List
from loguru import logger

from text_generation_server.utils.import_utils import SYSTEM
from text_generation_server.utils.weights import (
    Weight,
    WeightsLoader,
    UnquantizedWeight,
    Weights,
)
from text_generation_server.utils.log import log_master, log_once
import importlib.util


FBGEMM_MM_AVAILABLE = False
FBGEMM_DYN_AVAILABLE = False

# Input scaling factors are no longer optional in _scaled_mm starting
# from pytorch 2.5. Allocating a dummy tensor to pass as input_scale
TORCH_DEVICE_IDENTITY = torch.ones(1).cuda() if SYSTEM == "rocm" else None


def is_fbgemm_gpu_available():
    try:
        return importlib.util.find_spec("fbgemm_gpu.experimental.gen_ai") is not None
    except ModuleNotFoundError:
        return False


if is_fbgemm_gpu_available():
    if SYSTEM == "cuda":
        major, _ = torch.cuda.get_device_capability()
        FBGEMM_MM_AVAILABLE = major == 9
        FBGEMM_DYN_AVAILABLE = major >= 8
else:
    log_master(logger.warning, "FBGEMM fp8 kernels are not installed.")


def get_fp8_linear() -> torch.nn.Module:
    """
    Return an FP8 linear `Module` that is compatible with the current system.
    """

    if SYSTEM == "cuda":
        major, _ = torch.cuda.get_device_capability()
        if major == 8:
            from text_generation_server.layers.marlin import GPTQMarlinFP8Linear

            return GPTQMarlinFP8Linear

    # On other systems let Torch decide if the hardware supports FP8.
    return Fp8Linear


def normalize_e4m3fn_to_e4m3fnuz(
    weight: torch.Tensor,
    weight_scale: torch.Tensor,
    input_scale: Optional[torch.Tensor] = None,
) -> Tuple[torch.Tensor, torch.Tensor, Optional[torch.Tensor]]:
    assert weight.dtype == torch.float8_e4m3fn
    # The bits pattern 10000000(-128) represents zero in e4m3fn
    # but NaN in e4m3fnuz. So here we set it to 0.
    # https://onnx.ai/onnx/technical/float8.html
    weight_as_int8 = weight.view(torch.int8)
    ROCM_FP8_NAN_AS_INT = -128
    weight_as_int8[weight_as_int8 == ROCM_FP8_NAN_AS_INT] = 0
    weight = weight_as_int8.view(torch.float8_e4m3fnuz)

    # For the same bits representation, e4m3fnuz value is half of
    # the e4m3fn value, so we should double the scaling factor to
    # get the same dequantized value.
    # https://onnx.ai/onnx/technical/float8.html
    weight_scale = weight_scale * 2.0
    if input_scale is not None:
        input_scale = input_scale * 2.0
    return weight, weight_scale, input_scale


def fp8_quantize(
    weight, scale=None, scale_upper_bound=None, qdtype=torch.float8_e4m3fn, scalar=False
):
    if FBGEMM_DYN_AVAILABLE and not scalar:
        qweight, scale = torch.ops.fbgemm.quantize_fp8_per_row(
            weight, bs=None, scale_ub=scale_upper_bound, output_dtype=qdtype
        )
        return qweight, scale

    # weight, scale = quant_weights(weight, torch.int8, False)
    finfo = torch.finfo(qdtype)

    if scale is None:
        # Calculate the scale as dtype max divided by absmax
        scale = finfo.max / weight.abs().max().clamp(min=1e-12, max=scale_upper_bound)
    else:
        logger.info("Using provided scale for quantization")

    # scale and clamp the tensor to bring it to
    # the representative range of float8 data type
    # (as default cast is unsaturated)
    qweight = (weight * scale).clamp(min=finfo.min, max=finfo.max)
    # Return both float8 data and the inverse scale (as float),
    # as both required as inputs to torch._scaled_mm
    qweight = qweight.to(qdtype)
    scale = scale.float().reciprocal()

    if SYSTEM == "rocm":
        qweight, scale, _ = normalize_e4m3fn_to_e4m3fnuz(qweight, scale)

    return qweight, scale


class HybridFP8UnquantLoader(WeightsLoader):
    """Weight loader that loads FP8 and unquantized Torch tensors."""

    def __init__(self, activation_scale_ub: Optional[float], to_fp8: bool):
        self.activation_scale_ub = activation_scale_ub
        self.to_fp8 = to_fp8

    def get_weights(self, weights: "Weights", prefix: str):
        w = weights.get_tensor(f"{prefix}.weight")

        if w.dtype == torch.float8_e4m3fn:
            # FP8 branch
            scale = (
                weights.get_tensor(f"{prefix}.weight_scale", to_dtype=False)
                .reshape(-1)
                .expand(w.shape[0])
            )
            try:
                input_scale = weights.get_tensor(
                    f"{prefix}.input_scale", to_dtype=False
                ).reshape(-1)
            except Exception:
                input_scale = None

            return Fp8Weight(
                weight=w,
                weight_scale=scale,
                input_scale=input_scale,
                activation_scale_ub=self.activation_scale_ub,
                dtype=weights.dtype,
            )
        if self.to_fp8:
            return Fp8Weight(weight=w, dtype=weights.dtype)

        return UnquantizedWeight(w)

    def get_weights_col_packed(
        self,
        weights: Weights,
        prefix: str,
        block_sizes: Union[int, List[int]],
    ):
        w = weights.get_packed_sharded(
            f"{prefix}.weight", dim=0, block_sizes=block_sizes
        )

        if w.dtype == torch.float8_e4m3fn:
            # FP8 branch
            scale = weights.get_tensor(f"{prefix}.weight_scale", to_dtype=False)
            if scale.numel() > 1:
                scale = weights.get_packed_sharded(
                    f"{prefix}.weight_scale",
                    dim=0,
                    block_sizes=block_sizes,
                    to_dtype=False,
                )
            scale = scale.reshape(-1).expand(w.shape[0])
            try:
                input_scale = weights.get_tensor(
                    f"{prefix}.input_scale", to_dtype=False
                )
                if input_scale.numel() > 1:
                    input_scale = weights.get_packed_sharded(
                        f"{prefix}.input_scale",
                        dim=0,
                        block_sizes=block_sizes,
                        to_dtype=False,
                    )
                input_scale = input_scale.reshape(-1).max()
            except Exception:
                input_scale = None

            return Fp8Weight(
                weight=w,
                weight_scale=scale,
                input_scale=input_scale,
                activation_scale_ub=self.activation_scale_ub,
                dtype=weights.dtype,
            )
        if self.to_fp8:
            return Fp8Weight(weight=w, dtype=weights.dtype)

        return UnquantizedWeight(w)

    def get_multi_weights_col(self, weights: "Weights", prefixes: List[str], dim: int):
        # FIXME: Force to_device to false as fp8 weights do not support torch.cat on device yet
        w = [
            weights.get_sharded(f"{p}.weight", dim=0, to_device=False) for p in prefixes
        ]
        shapes = [x.shape for x in w]

        # Concat then send to the device
        w = torch.cat(w, dim=dim).to(weights.device)

        # FP8 branch
        if w.dtype == torch.float8_e4m3fn:
            scale = [
                _load_scalar_or_matrix_scale(weights, f"{p}.weight_scale", shape)
                for p, shape in zip(prefixes, shapes)
            ]
            scale = torch.cat(scale, dim=0).reshape(-1)
            try:
                input_scale = [
                    _load_scalar_or_matrix_scale(weights, f"{p}.input_scale", shape)
                    for p, shape in zip(prefixes, shapes)
                ]
                input_scale = torch.cat(input_scale, dim=0).reshape(-1).max()
            except Exception:
                input_scale = None

            return Fp8Weight(
                weight=w,
                weight_scale=scale,
                input_scale=input_scale,
                activation_scale_ub=self.activation_scale_ub,
                dtype=weights.dtype,
            )
        if self.to_fp8:
            return Fp8Weight(weight=w, dtype=weights.dtype)

        return UnquantizedWeight(w)

    def get_weights_row(self, weights: "Weights", prefix: str):
        w = weights.get_sharded(f"{prefix}.weight", dim=1)
        # FP8 branch
        if w.dtype == torch.float8_e4m3fn:
            scale = (
                weights.get_tensor(f"{prefix}.weight_scale", to_dtype=False)
                .reshape(-1)
                .expand(w.shape[0])
            )
            try:
                input_scale = weights.get_tensor(
                    f"{prefix}.input_scale", to_dtype=False
                ).reshape(-1)
            except Exception:
                input_scale = None

            return Fp8Weight(
                weight=w,
                weight_scale=scale,
                input_scale=input_scale,
                activation_scale_ub=self.activation_scale_ub,
                dtype=weights.dtype,
            )
        if self.to_fp8:
            return Fp8Weight(weight=w, dtype=weights.dtype)

        return UnquantizedWeight(w)


@dataclass
class Fp8Weight(Weight):
    weight: torch.Tensor
    dtype: torch.dtype
    weight_scale: Optional[torch.Tensor] = None
    input_scale: Optional[torch.Tensor] = None
    activation_scale_ub: Optional[float] = None

    def get_linear(self, bias: torch.Tensor):
        if self.weight_scale is None:
            return get_fp8_linear().from_unquant(self.weight, bias, self.dtype)
        # This is not checked by the fbgemm kernels, but they require contiguous
        # memory. Can be non-contiguous when we e.g. expand from scalars.
        self.weight_scale = self.weight_scale.contiguous()
        return get_fp8_linear().from_fp8(
            self.weight,
            self.weight_scale,
            self.input_scale,
            self.activation_scale_ub,
            bias,
            self.dtype,
        )


class Fp8Linear(torch.nn.Module):
    def __init__(
        self,
        qweight,
        scale,
        input_scale,
        scale_upper_bound,
        bias,
        dtype,
    ) -> None:
        super().__init__()
        if FBGEMM_MM_AVAILABLE:
            log_once(logger.info, "Using FBGEMM fp8 optimized kernels")

        if SYSTEM == "rocm":
            qweight, scale, _ = normalize_e4m3fn_to_e4m3fnuz(
                weight=qweight, weight_scale=scale
            )

        self.dtype = dtype
        self.qweight = qweight
        self.scale = scale.float()
        self.input_scale = (
            input_scale.float().reciprocal() if input_scale is not None else None
        )

        if FBGEMM_MM_AVAILABLE:
            self.scale_upper_bound = (
                torch.tensor(
                    [scale_upper_bound], dtype=torch.float32, device=qweight.device
                )
                if scale_upper_bound is not None
                else None
            )
        else:
            self.scale_upper_bound = scale_upper_bound

        self.bias = bias if bias is not None else None

    @classmethod
    def from_unquant(cls, weight, bias, dtype):
        qweight, scale = fp8_quantize(weight, scalar=not FBGEMM_MM_AVAILABLE)
        return cls(
            qweight=qweight, scale=scale, scale_upper_bound=None, bias=bias, dtype=dtype
        )

    @classmethod
    def from_fp8(cls, weight, scale, input_scale, scale_upper_bound, bias, dtype):
        if FBGEMM_DYN_AVAILABLE:
            # fbgemm needs float32 scales.
            scale = scale.float()
        return cls(
            qweight=weight,
            scale=scale,
            input_scale=input_scale,
            scale_upper_bound=scale_upper_bound,
            bias=bias,
            dtype=dtype,
        )

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        if FBGEMM_MM_AVAILABLE:
            qinput, scale = fp8_quantize(
                input, scale_upper_bound=self.scale_upper_bound
            )

            y = torch.ops.fbgemm.f8f8bf16_rowwise(
                qinput,
                self.qweight,
                scale,
                self.scale,
                use_fast_accum=True,
                bias=self.bias,
            )
            return y.to(self.dtype)

        qinput, scale = fp8_quantize(
            input,
            self.input_scale,
            scale_upper_bound=self.scale_upper_bound,
            scalar=True,
        )

        per_tensor_weights = self.scale.numel() == 1
        per_tensor_activations = scale.numel() == 1

        if per_tensor_weights and per_tensor_activations:
            output = torch._scaled_mm(
                qinput,
                self.qweight.t(),
                out_dtype=self.dtype,
                scale_a=scale,
                scale_b=self.scale,
                bias=self.bias,
            )

            if type(output) is tuple and len(output) == 2:
                output = output[0]
        else:
            global TORCH_DEVICE_IDENTITY
            if TORCH_DEVICE_IDENTITY.device != self.qweight.device:
                TORCH_DEVICE_IDENTITY = TORCH_DEVICE_IDENTITY.to(self.qweight.device)
            output = torch._scaled_mm(
                qinput,
                self.qweight.t(),
                scale_a=TORCH_DEVICE_IDENTITY,
                scale_b=TORCH_DEVICE_IDENTITY,
                out_dtype=torch.float32,
            )
            if type(output) is tuple and len(output) == 2:
                output = output[0]

            output = output * scale * self.scale.t()
            if self.bias is not None:
                output = output + self.bias

            output = output.to(dtype=self.dtype)

        return output


def _load_scalar_or_matrix_scale(weights: Weights, prefix: str, shape: torch.Size):
    scale = weights.get_tensor(prefix, to_dtype=False)
    if scale.numel() > 1:
        scale = weights.get_sharded(prefix, dim=0, to_dtype=False)
    return scale.reshape(-1).expand(shape[0])
