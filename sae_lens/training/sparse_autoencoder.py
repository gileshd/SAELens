"""Most of this is just copied over from Arthur's code and slightly simplified:
https://github.com/ArthurConmy/sae/blob/main/sae/model.py
"""

import json
import os
from typing import Callable, NamedTuple, Optional

import einops
import torch
from jaxtyping import Float
from safetensors.torch import save_file
from torch import nn
from transformer_lens.hook_points import HookedRootModule, HookPoint

from sae_lens.toolkit.pretrained_sae_loaders import (
    NAMED_PRETRAINED_SAE_LOADERS,
    load_pretrained_sae_lens_sae_components,
)
from sae_lens.toolkit.pretrained_saes_directory import get_pretrained_saes_directory
from sae_lens.training.activation_functions import get_activation_fn
from sae_lens.training.config import LanguageModelSAERunnerConfig

SPARSITY_PATH = "sparsity.safetensors"
SAE_WEIGHTS_PATH = "sae_weights.safetensors"
SAE_CFG_PATH = "cfg.json"


class ForwardOutput(NamedTuple):
    sae_out: torch.Tensor
    feature_acts: torch.Tensor
    loss: torch.Tensor
    mse_loss: torch.Tensor
    l1_loss: torch.Tensor
    ghost_grad_loss: torch.Tensor | float


class SparseAutoencoderBase(HookedRootModule):
    """ """

    # forward pass details.
    d_in: int
    d_sae: int
    activation_fn_str: str
    activation_fn: Callable[[torch.Tensor], torch.Tensor]
    apply_b_dec_to_input: bool
    uses_scaling_factor: bool

    # dataset it was trained on details.
    context_size: int
    model_name: str
    hook_point: str
    hook_point_layer: int
    hook_point_head_index: Optional[int]
    prepend_bos: bool
    dataset_path: str

    # misc
    dtype: torch.dtype
    device: str | torch.device
    sae_lens_training_version: Optional[str]

    def __init__(
        self,
        d_in: int,
        d_sae: int,
        dtype: torch.dtype,
        device: str | torch.device,
        model_name: str,
        hook_point: str,
        hook_point_layer: int,
        hook_point_head_index: Optional[int] = None,
        activation_fn: str = "relu",
        apply_b_dec_to_input: bool = True,
        uses_scaling_factor: bool = False,
        sae_lens_training_version: Optional[str] = None,
        prepend_bos: bool = True,
        dataset_path: str = "unknown",
        context_size: int = 256,
    ):
        super().__init__()

        self.d_in = d_in
        self.d_sae = d_sae  # type: ignore
        self.activation_fn_str = activation_fn
        self.activation_fn = get_activation_fn(activation_fn)
        self.apply_b_dec_to_input = apply_b_dec_to_input
        self.uses_scaling_factor = uses_scaling_factor

        self.model_name = model_name
        self.hook_point = hook_point
        self.hook_point_layer = hook_point_layer
        self.hook_point_head_index = hook_point_head_index
        self.dataset_name = dataset_path
        self.prepend_bos = prepend_bos
        self.context_size = context_size

        self.dtype = dtype
        self.device = device
        self.sae_lens_training_version = sae_lens_training_version

        self.initialize_weights_basic()

        # handle presence / absence of scaling factor.
        if self.uses_scaling_factor:
            self.apply_scaling_factor = lambda x: x * self.scaling_factor
        else:
            self.apply_scaling_factor = lambda x: x

        # set up hooks
        self.hook_sae_in = HookPoint()
        self.hook_hidden_pre = HookPoint()
        self.hook_hidden_post = HookPoint()
        self.hook_sae_out = HookPoint()

        self.setup()  # Required for `HookedRootModule`s

    def initialize_weights_basic(self):

        # no config changes encoder bias init for now.
        self.b_enc = nn.Parameter(
            torch.zeros(self.d_sae, dtype=self.dtype, device=self.device)
        )

        # Start with the default init strategy:
        self.W_dec = nn.Parameter(
            torch.nn.init.kaiming_uniform_(
                torch.empty(self.d_sae, self.d_in, dtype=self.dtype, device=self.device)
            )
        )

        self.W_enc = nn.Parameter(
            torch.nn.init.kaiming_uniform_(
                torch.empty(self.d_in, self.d_sae, dtype=self.dtype, device=self.device)
            )
        )

        # methdods which change b_dec as a function of the dataset are implemented after init.
        self.b_dec = nn.Parameter(
            torch.zeros(self.d_in, dtype=self.dtype, device=self.device)
        )

        # scaling factor for fine-tuning (not to be used in initial training)
        # TODO: Make this optional and not included with all SAEs by default (but maintain backwards compatibility)
        if self.uses_scaling_factor:
            self.scaling_factor = nn.Parameter(
                torch.ones(self.d_sae, dtype=self.dtype, device=self.device)
            )

    # Basic Forward Pass Functionality.
    def forward(
        self,
        x: torch.Tensor,
    ) -> torch.Tensor:

        feature_acts = self.encode(x)
        sae_out = self.decode(feature_acts)

        return sae_out

    def encode(
        self, x: Float[torch.Tensor, "... d_in"]
    ) -> Float[torch.Tensor, "... d_sae"]:

        # move x to correct dtype
        x = x.to(self.dtype)

        # apply b_dec_to_input if using that method.
        sae_in = self.hook_sae_in(x - (self.b_dec * self.apply_b_dec_to_input))

        # "... d_in, d_in d_sae -> ... d_sae",
        hidden_pre = self.hook_hidden_pre(sae_in @ self.W_enc + self.b_enc)
        feature_acts = self.hook_hidden_post(self.activation_fn(hidden_pre))

        return feature_acts

    def decode(
        self, feature_acts: Float[torch.Tensor, "... d_sae"]
    ) -> Float[torch.Tensor, "... d_in"]:
        """Decodes SAE feature activation tensor into a reconstructed input activation tensor."""
        # "... d_sae, d_sae d_in -> ... d_in",
        sae_out = self.hook_sae_out(
            self.apply_scaling_factor(feature_acts) @ self.W_dec + self.b_dec
        )
        return sae_out

    def save_model(self, path: str, sparsity: Optional[torch.Tensor] = None):

        if not os.path.exists(path):
            os.mkdir(path)

        # generate the weights
        save_file(self.state_dict(), f"{path}/{SAE_WEIGHTS_PATH}")

        # save the config
        config = self.get_config_dict()

        with open(f"{path}/{SAE_CFG_PATH}", "w") as f:
            json.dump(config, f)

        if sparsity is not None:
            sparsity_in_dict = {"sparsity": sparsity}
            save_file(sparsity_in_dict, f"{path}/{SPARSITY_PATH}")  # type: ignore

    @classmethod
    def load_from_pretrained(
        cls, path: str, device: str = "cpu", dtype: torch.dtype = torch.float32
    ) -> "SparseAutoencoderBase":

        config_path = os.path.join(path, "cfg.json")
        weight_path = os.path.join(path, "sae_weights.safetensors")

        cfg_dict, state_dict = load_pretrained_sae_lens_sae_components(
            config_path, weight_path, device, dtype
        )

        sae = cls(
            d_in=cfg_dict["d_in"],
            d_sae=cfg_dict["d_sae"],
            dtype=cfg_dict["dtype"],
            device=cfg_dict["device"],
            model_name=cfg_dict["model_name"],
            hook_point=cfg_dict["hook_point"],
            hook_point_layer=cfg_dict["hook_point_layer"],
            hook_point_head_index=cfg_dict["hook_point_head_index"],
            activation_fn=cfg_dict["activation_fn"],
        )

        sae.load_state_dict(state_dict)

        return sae

    @classmethod
    def from_pretrained(
        cls, release: str, sae_id: str, device: str = "cpu"
    ) -> "SparseAutoencoderBase":
        """

        Load a pretrained SAE from the Hugging Face model hub.

        Args:
            release: The release name. This will be mapped to a huggingface repo id based on the pretrained_saes.yaml file.
            id: The id of the SAE to load. This will be mapped to a path in the huggingface repo.
            device: The device to load the SAE on.

        """

        # get sae directory
        sae_directory = get_pretrained_saes_directory()

        # get the repo id and path to the SAE
        if release not in sae_directory:
            raise ValueError(
                f"Release {release} not found in pretrained SAEs directory."
            )
        if sae_id not in sae_directory[release].saes_map:
            raise ValueError(f"ID {sae_id} not found in release {release}.")
        sae_info = sae_directory[release]
        hf_repo_id = sae_info.repo_id
        hf_path = sae_info.saes_map[sae_id]

        conversion_loader_name = sae_info.conversion_func or "sae_lens"
        if conversion_loader_name not in NAMED_PRETRAINED_SAE_LOADERS:
            raise ValueError(
                f"Conversion func {conversion_loader_name} not found in NAMED_PRETRAINED_SAE_LOADERS."
            )
        conversion_loader = NAMED_PRETRAINED_SAE_LOADERS[conversion_loader_name]

        cfg_dict, state_dict = conversion_loader(
            repo_id=hf_repo_id,
            folder_name=hf_path,
            device=device,
            force_download=False,
        )

        if "prepend_bos" not in cfg_dict:
            # default to True for backwards compatibility
            cfg_dict["prepend_bos"] = True

        sae = cls(
            d_in=cfg_dict["d_in"],
            d_sae=cfg_dict["d_sae"],
            dtype=cfg_dict["dtype"],
            device=cfg_dict["device"],
            model_name=cfg_dict["model_name"],
            hook_point=cfg_dict["hook_point"],
            hook_point_layer=cfg_dict["hook_point_layer"],
            hook_point_head_index=cfg_dict["hook_point_head_index"],
            activation_fn=(
                cfg_dict["activation_fn"] if "activation_fn" in cfg_dict else "relu"
            ),
            context_size=cfg_dict["context_size"],
            dataset_path=cfg_dict["dataset_path"],
            prepend_bos=cfg_dict["prepend_bos"],
        )
        sae.load_state_dict(state_dict)

        return sae

    def get_name(self):
        sae_name = (
            f"sparse_autoencoder_{self.model_name}_{self.hook_point}_{self.d_sae}"
        )
        return sae_name

    def get_config_dict(self):
        return {
            "d_in": self.d_in,
            "d_sae": self.d_sae,
            "dtype": str(self.dtype),
            "device": str(self.device),
            "model_name": self.model_name,
            "hook_point": self.hook_point,
            "hook_point_layer": self.hook_point_layer,
            "hook_point_head_index": self.hook_point_head_index,
            "activation_fn": self.activation_fn_str,  # use string for serialization
            "act_store_device": str(self.cfg.act_store_device),
            "apply_b_dec_to_input": self.apply_b_dec_to_input,
            "uses_scaling_factor": self.uses_scaling_factor,
            "sae_lens_training_version": self.sae_lens_training_version,
            "prepend_bos": self.prepend_bos,
            "dataset_name": self.dataset_name,
    }


class TrainingSparseAutoencoder(SparseAutoencoderBase):

    l1_coefficient: float
    lp_norm: float
    use_ghost_grads: bool
    normalize_sae_decoder: bool
    noise_scale: float
    decoder_orthogonal_init: bool
    mse_loss_normalization: Optional[str]

    def __init__(self, cfg: LanguageModelSAERunnerConfig):

        super().__init__(
            d_in=cfg.d_in,
            d_sae=cfg.d_sae,  # type: ignore
            dtype=cfg.dtype,
            device=cfg.device,
            model_name=cfg.model_name,
            hook_point=cfg.hook_point,
            hook_point_layer=cfg.hook_point_layer,
            hook_point_head_index=cfg.hook_point_head_index,
            activation_fn=cfg.activation_fn,
            apply_b_dec_to_input=cfg.apply_b_dec_to_input,
            uses_scaling_factor=cfg.finetuning_method is not None,
            sae_lens_training_version=cfg.sae_lens_training_version,
        )

        self.mse_loss_normalization = cfg.mse_loss_normalization
        self.l1_coefficient = cfg.l1_coefficient
        self.lp_norm = cfg.lp_norm
        self.scale_sparsity_penalty_by_decoder_norm = (
            cfg.scale_sparsity_penalty_by_decoder_norm
        )
        self.use_ghost_grads = cfg.use_ghost_grads
        self.noise_scale = cfg.noise_scale

        self.normalize_sae_decoder = cfg.normalize_sae_decoder
        self.decoder_orthogonal_init = cfg.decoder_orthogonal_init
        self.decoder_heuristic_init = cfg.decoder_heuristic_init
        self.init_encoder_as_decoder_transpose = cfg.init_encoder_as_decoder_transpose

        self.initialize_weights_complex()

        self.get_sparsity_loss_term = (
            self.get_sparsity_loss_term_decoder_norm
            if self.scale_sparsity_penalty_by_decoder_norm
            else self.get_sparsity_loss_term_standard
        )

    def encode(
        self, x: Float[torch.Tensor, "... d_in"]
    ) -> Float[torch.Tensor, "... d_sae"]:
        feature_acts, _ = self._encode_with_hidden_pre(x)
        return feature_acts

    # needed for ghost grads.
    def _encode_with_hidden_pre(
        self, x: Float[torch.Tensor, "... d_in"]
    ) -> tuple[Float[torch.Tensor, "... d_sae"], Float[torch.Tensor, "... d_sae"]]:
        """Encodes input activation tensor x into an SAE feature activation tensor."""

        # move x to correct dtype
        x = x.to(self.dtype)
        sae_in = self.hook_sae_in(
            x - (self.b_dec * self.apply_b_dec_to_input)
        )  # Remove decoder bias as per Anthropic

        # "... d_in, d_in d_sae -> ... d_sae",
        hidden_pre = self.hook_hidden_pre(sae_in @ self.W_enc + self.b_enc)

        # Key difference for training SAE.
        noisy_hidden_pre = hidden_pre + (
            torch.randn_like(hidden_pre) * self.noise_scale * self.training
        )  # noise scale will be 0 by default, and should be 0 if not in training.

        feature_acts = self.hook_hidden_post(self.activation_fn(noisy_hidden_pre))

        return feature_acts, hidden_pre

    def forward(  # type: ignore (override intentionally) since we want different training / inference behavior)
        self, x: torch.Tensor, dead_neuron_mask: torch.Tensor | None = None
    ) -> ForwardOutput:

        feature_acts, hidden_pre = self._encode_with_hidden_pre(x)
        sae_out = self.decode(feature_acts)

        # add config for whether l2 is normalized:
        per_item_mse_loss = self._per_item_mse_loss_with_target_norm(
            sae_out, x, self.mse_loss_normalization
        )

        # gate on config and training so evals is not slowed down.
        if (
            self.use_ghost_grads
            and self.training
            and dead_neuron_mask is not None
            and dead_neuron_mask.sum() > 0
        ):
            ghost_grad_loss = self.calculate_ghost_grad_loss(
                x=x,
                sae_out=sae_out,
                per_item_mse_loss=per_item_mse_loss,
                hidden_pre=hidden_pre,
                dead_neuron_mask=dead_neuron_mask,
            )
        else:
            ghost_grad_loss = 0

        mse_loss = per_item_mse_loss.sum(dim=-1).mean()
        sparsity = self.get_sparsity_loss_term(feature_acts)
        l1_loss = (self.l1_coefficient * sparsity).mean()
        loss = mse_loss + l1_loss + ghost_grad_loss

        return ForwardOutput(
            sae_out=sae_out,
            feature_acts=feature_acts,
            loss=loss,
            mse_loss=mse_loss,
            l1_loss=l1_loss,
            ghost_grad_loss=ghost_grad_loss,
        )

    def initialize_weights_complex(self):
        """ """

        if self.decoder_orthogonal_init:
            self.W_dec.data = nn.init.orthogonal_(self.W_dec.data.T).T

        elif self.decoder_heuristic_init:
            self.W_dec = nn.Parameter(
                torch.rand(self.d_sae, self.d_in, dtype=self.dtype, device=self.device)
            )
            self.initialize_decoder_norm_constant_norm()

        elif self.normalize_sae_decoder:
            self.set_decoder_norm_to_unit_norm()

        # Then we intialize the encoder weights (either as the transpose of decoder or not)
        if self.init_encoder_as_decoder_transpose:
            self.W_enc.data = self.W_dec.data.T.clone().contiguous()
        else:
            self.W_enc = nn.Parameter(
                torch.nn.init.kaiming_uniform_(
                    torch.empty(
                        self.d_in, self.d_sae, dtype=self.dtype, device=self.device
                    )
                )
            )

        if self.normalize_sae_decoder:
            with torch.no_grad():
                # Anthropic normalize this to have unit columns
                self.set_decoder_norm_to_unit_norm()

    ## Loss Function Utils
    def get_sparsity_loss_term_standard(
        self, feature_acts: torch.Tensor
    ) -> torch.Tensor:
        """
        Sparsity loss term calculated as the L1 norm of the feature activations.
        """
        sparsity = feature_acts.norm(p=self.lp_norm, dim=-1)
        return sparsity

    def get_sparsity_loss_term_decoder_norm(
        self, feature_acts: torch.Tensor
    ) -> torch.Tensor:
        """
        Sparsity loss term for decoder norm regularization.
        """
        weighted_feature_acts = feature_acts * self.W_dec.norm(dim=1)
        sparsity = weighted_feature_acts.norm(
            p=self.lp_norm, dim=-1
        )  # sum over the feature dimension
        return sparsity

    def _per_item_mse_loss_with_target_norm(
        self,
        preds: torch.Tensor,
        target: torch.Tensor,
        mse_loss_normalization: Optional[str] = None,
    ) -> torch.Tensor:
        """
        Calculate MSE loss per item in the batch, without taking a mean.
        Then, optionally, normalizes by the L2 norm of the centered target.
        This normalization seems to improve performance.
        """
        if mse_loss_normalization == "dense_batch":
            target_centered = target - target.mean(dim=0, keepdim=True)
            normalization = target_centered.norm(dim=-1, keepdim=True)
            return torch.nn.functional.mse_loss(preds, target, reduction="none") / (
                normalization + 1e-6
            )
        else:
            return torch.nn.functional.mse_loss(preds, target, reduction="none")

    def calculate_ghost_grad_loss(
        self,
        x: torch.Tensor,
        sae_out: torch.Tensor,
        per_item_mse_loss: torch.Tensor,
        hidden_pre: torch.Tensor,
        dead_neuron_mask: torch.Tensor,
    ) -> torch.Tensor:
        # 1.
        residual = x - sae_out
        l2_norm_residual = torch.norm(residual, dim=-1)

        # 2.
        feature_acts_dead_neurons_only = torch.exp(hidden_pre[:, dead_neuron_mask])
        ghost_out = feature_acts_dead_neurons_only @ self.W_dec[dead_neuron_mask, :]
        l2_norm_ghost_out = torch.norm(ghost_out, dim=-1)
        norm_scaling_factor = l2_norm_residual / (1e-6 + l2_norm_ghost_out * 2)
        ghost_out = ghost_out * norm_scaling_factor[:, None].detach()

        # 3.
        per_item_mse_loss_ghost_resid = self._per_item_mse_loss_with_target_norm(
            ghost_out, residual.detach(), self.mse_loss_normalization
        )
        mse_rescaling_factor = (
            per_item_mse_loss / (per_item_mse_loss_ghost_resid + 1e-6)
        ).detach()
        per_item_mse_loss_ghost_resid = (
            mse_rescaling_factor * per_item_mse_loss_ghost_resid
        )

        return per_item_mse_loss_ghost_resid.mean()

    ## Initialization Methods
    @torch.no_grad()
    def initialize_b_dec_with_precalculated(self, origin: torch.Tensor):
        out = torch.tensor(origin, dtype=self.dtype, device=self.device)
        self.b_dec.data = out

    @torch.no_grad()
    def initialize_b_dec_with_mean(self, all_activations: torch.Tensor):
        previous_b_dec = self.b_dec.clone().cpu()
        out = all_activations.mean(dim=0)

        previous_distances = torch.norm(all_activations - previous_b_dec, dim=-1)
        distances = torch.norm(all_activations - out, dim=-1)

        print("Reinitializing b_dec with mean of activations")
        print(
            f"Previous distances: {previous_distances.median(0).values.mean().item()}"
        )
        print(f"New distances: {distances.median(0).values.mean().item()}")

        self.b_dec.data = out.to(self.dtype).to(self.device)

    ## Training Utils
    @torch.no_grad()
    def set_decoder_norm_to_unit_norm(self):
        self.W_dec.data /= torch.norm(self.W_dec.data, dim=1, keepdim=True)

    @torch.no_grad()
    def initialize_decoder_norm_constant_norm(self, norm: float = 0.1):
        """
        A heuristic proceedure inspired by:
        https://transformer-circuits.pub/2024/april-update/index.html#training-saes
        """
        # TODO: Parameterise this as a function of m and n

        # ensure W_dec norms at unit norm
        self.W_dec.data /= torch.norm(self.W_dec.data, dim=1, keepdim=True)
        self.W_dec.data *= norm  # will break tests but do this for now.

    @torch.no_grad()
    def remove_gradient_parallel_to_decoder_directions(self):
        """
        Update grads so that they remove the parallel component
            (d_sae, d_in) shape
        """
        assert self.W_dec.grad is not None  # keep pyright happy

        parallel_component = einops.einsum(
            self.W_dec.grad,
            self.W_dec.data,
            "d_sae d_in, d_sae d_in -> d_sae",
        )
        self.W_dec.grad -= einops.einsum(
            parallel_component,
            self.W_dec.data,
            "d_sae, d_sae d_in -> d_sae d_in",
        )
