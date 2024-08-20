# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any, Dict, Optional, Union

import paddle
import paddle.nn as nn

from ..configuration_utils import ConfigMixin, register_to_config

# from ..loaders import FromOriginalModelMixin, PeftAdapterMixin
from ..models.attention import JointTransformerBlock
from ..models.attention_processor import Attention, AttentionProcessor
from ..models.modeling_utils import ModelMixin
from ..models.normalization import AdaLayerNormContinuous
from ..utils import (  # recompute_use_reentrant,; use_old_recompute,
    USE_PEFT_BACKEND,
    logging,
    scale_lora_layers,
    unscale_lora_layers,
)
from .embeddings import CombinedTimestepTextProjEmbeddings, PatchEmbed
from .simplified_sd3 import SimplifiedSD3
from .transformer_2d import Transformer2DModelOutput

# from paddle.distributed.fleet.utils import recompute


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


class SD3Transformer2DModel(ModelMixin, ConfigMixin):  # , PeftAdapterMixin, FromOriginalModelMixin
    """
    The Transformer model introduced in Stable Diffusion 3.
    Reference: https://arxiv.org/abs/2403.03206
    Parameters:
        sample_size (`int`): The width of the latent images. This is fixed during training since
            it is used to learn a number of position embeddings.
        patch_size (`int`): Patch size to turn the input data into small patches.
        in_channels (`int`, *optional*, defaults to 16): The number of channels in the input.
        num_layers (`int`, *optional*, defaults to 18): The number of layers of Transformer blocks to use.
        attention_head_dim (`int`, *optional*, defaults to 64): The number of channels in each head.
        num_attention_heads (`int`, *optional*, defaults to 18): The number of heads to use for multi-head attention.
        cross_attention_dim (`int`, *optional*): The number of `encoder_hidden_states` dimensions to use.
        caption_projection_dim (`int`): Number of dimensions to use when projecting the `encoder_hidden_states`.
        pooled_projection_dim (`int`): Number of dimensions to use when projecting the `pooled_projections`.
        out_channels (`int`, defaults to 16): Number of output channels.
    """

    _supports_gradient_checkpointing = True

    @register_to_config
    def __init__(
        self,
        sample_size: int = 128,
        patch_size: int = 2,
        in_channels: int = 16,
        num_layers: int = 18,
        attention_head_dim: int = 64,
        num_attention_heads: int = 18,
        joint_attention_dim: int = 4096,
        caption_projection_dim: int = 1152,
        pooled_projection_dim: int = 2048,
        out_channels: int = 16,
        pos_embed_max_size: int = 96,
    ):
        super().__init__()
        default_out_channels = in_channels
        self.out_channels = out_channels if out_channels is not None else default_out_channels
        self.inner_dim = self.config.num_attention_heads * self.config.attention_head_dim

        self.pos_embed = PatchEmbed(
            height=self.config.sample_size,
            width=self.config.sample_size,
            patch_size=self.config.patch_size,
            in_channels=self.config.in_channels,
            embed_dim=self.inner_dim,
            pos_embed_max_size=pos_embed_max_size,  # hard-code for now.
        )
        self.time_text_embed = CombinedTimestepTextProjEmbeddings(
            embedding_dim=self.inner_dim, pooled_projection_dim=self.config.pooled_projection_dim
        )
        self.context_embedder = nn.Linear(self.config.joint_attention_dim, self.config.caption_projection_dim)

        # `attention_head_dim` is doubled to account for the mixing.
        # It needs to crafted when we get the actual checkpoints.
        self.transformer_blocks = nn.LayerList(
            [
                JointTransformerBlock(
                    dim=self.inner_dim,
                    num_attention_heads=self.config.num_attention_heads,
                    attention_head_dim=self.inner_dim,
                    context_pre_only=i == num_layers - 1,
                )
                for i in range(self.config.num_layers)
            ]
        )

        self.simplified_sd3 = SimplifiedSD3(
            num_layers,
            dim=self.inner_dim,
            num_attention_heads=self.config.num_attention_heads,
            attention_head_dim=self.inner_dim,
            # context_pre_onl,
        )
        self.simplified_sd3 = paddle.incubate.jit.inference(
            self.simplified_sd3,
            enable_new_ir=True,
            cache_static_model=False,
            exp_enable_use_cutlass=True,
            delete_pass_lists=["add_norm_fuse_pass"],
        )

        self.norm_out = AdaLayerNormContinuous(self.inner_dim, self.inner_dim, elementwise_affine=False, eps=1e-6)
        self.proj_out = nn.Linear(self.inner_dim, patch_size * patch_size * self.out_channels, bias_attr=True)

        self.gradient_checkpointing = False

    # Copied from diffusers.models.unets.unet_3d_condition.UNet3DConditionModel.enable_forward_chunking
    def enable_forward_chunking(self, chunk_size: Optional[int] = None, dim: int = 0) -> None:
        """
        Sets the attention processor to use [feed forward
        chunking](https://huggingface.co/blog/reformer#2-chunked-feed-forward-layers).
        Parameters:
            chunk_size (`int`, *optional*):
                The chunk size of the feed-forward layers. If not specified, will run feed-forward layer individually
                over each tensor of dim=`dim`.
            dim (`int`, *optional*, defaults to `0`):
                The dimension over which the feed-forward computation should be chunked. Choose between dim=0 (batch)
                or dim=1 (sequence length).
        """
        if dim not in [0, 1]:
            raise ValueError(f"Make sure to set `dim` to either 0 or 1, not {dim}")

        # By default chunk size is 1
        chunk_size = chunk_size or 1

        def fn_recursive_feed_forward(module: paddle.nn.Layer, chunk_size: int, dim: int):
            if hasattr(module, "set_chunk_feed_forward"):
                module.set_chunk_feed_forward(chunk_size=chunk_size, dim=dim)

            for child in module.children():
                fn_recursive_feed_forward(child, chunk_size, dim)

        for module in self.children():
            fn_recursive_feed_forward(module, chunk_size, dim)

    @property
    # Copied from diffusers.models.unets.unet_2d_condition.UNet2DConditionModel.attn_processors
    def attn_processors(self) -> Dict[str, AttentionProcessor]:
        r"""
        Returns:
            `dict` of attention processors: A dictionary containing all attention processors used in the model with
            indexed by its weight name.
        """
        # set recursively
        processors = {}

        def fn_recursive_add_processors(
            name: str, module: paddle.nn.Module, processors: Dict[str, AttentionProcessor]
        ):
            if hasattr(module, "get_processor"):
                processors[f"{name}.processor"] = module.get_processor(return_deprecated_lora=True)

            for sub_name, child in module.named_children():
                fn_recursive_add_processors(f"{name}.{sub_name}", child, processors)

            return processors

        for name, module in self.named_children():
            fn_recursive_add_processors(name, module, processors)

        return processors

    # Copied from diffusers.models.unets.unet_2d_condition.UNet2DConditionModel.set_attn_processor
    def set_attn_processor(self, processor: Union[AttentionProcessor, Dict[str, AttentionProcessor]]):
        r"""
        Sets the attention processor to use to compute attention.
        Parameters:
            processor (`dict` of `AttentionProcessor` or only `AttentionProcessor`):
                The instantiated processor class or a dictionary of processor classes that will be set as the processor
                for **all** `Attention` layers.
                If `processor` is a dict, the key needs to define the path to the corresponding cross attention
                processor. This is strongly recommended when setting trainable attention processors.
        """
        count = len(self.attn_processors.keys())

        if isinstance(processor, dict) and len(processor) != count:
            raise ValueError(
                f"A dict of processors was passed, but the number of processors {len(processor)} does not match the"
                f" number of attention layers: {count}. Please make sure to pass {count} processor classes."
            )

        def fn_recursive_attn_processor(name: str, module: paddle.nn.Module, processor):
            if hasattr(module, "set_processor"):
                if not isinstance(processor, dict):
                    module.set_processor(processor)
                else:
                    module.set_processor(processor.pop(f"{name}.processor"))

            for sub_name, child in module.named_children():
                fn_recursive_attn_processor(f"{name}.{sub_name}", child, processor)

        for name, module in self.named_children():
            fn_recursive_attn_processor(name, module, processor)

    # Copied from diffusers.models.unets.unet_2d_condition.UNet2DConditionModel.fuse_qkv_projections
    def fuse_qkv_projections(self):
        """
        Enables fused QKV projections. For self-attention modules, all projection matrices (i.e., query, key, value)
        are fused. For cross-attention modules, key and value projection matrices are fused.
        <Tip warning={true}>
        This API is 🧪 experimental.
        </Tip>
        """
        self.original_attn_processors = None

        for _, attn_processor in self.attn_processors.items():
            if "Added" in str(attn_processor.__class__.__name__):
                raise ValueError("`fuse_qkv_projections()` is not supported for models having added KV projections.")

        self.original_attn_processors = self.attn_processors

        for module in self.modules():
            if isinstance(module, Attention):
                module.fuse_projections(fuse=True)

    # Copied from diffusers.models.unets.unet_2d_condition.UNet2DConditionModel.unfuse_qkv_projections
    def unfuse_qkv_projections(self):
        """Disables the fused QKV projection if enabled.
        <Tip warning={true}>
        This API is 🧪 experimental.
        </Tip>
        """
        if self.original_attn_processors is not None:
            self.set_attn_processor(self.original_attn_processors)

    def _set_gradient_checkpointing(self, module, value=False):
        if hasattr(module, "gradient_checkpointing"):
            module.gradient_checkpointing = value

    # @paddle.incubate.jit.inference(
    #     enable_new_ir=True,
    #     cache_static_model=False,
    #     switch_ir_optim=True,
    #     exp_enable_use_cutlass=True,
    #     delete_pass_lists=["add_norm_fuse_pass"],
    #     )
    # def sd3_transformer(
    #         self,
    #         hidden_states,
    #         encoder_hidden_states,
    #         temb,):
    #         # print(encoder_hidden_states)
    #     for block in self.transformer_blocks:
    #         encoder_hidden_states, hidden_states = block(
    #             hidden_states=hidden_states,
    #             encoder_hidden_states=encoder_hidden_states,
    #             temb=temb
    #             )
    #     return encoder_hidden_states, hidden_states

    def forward(
        self,
        hidden_states: paddle.Tensor,
        encoder_hidden_states: paddle.Tensor = None,
        pooled_projections: paddle.Tensor = None,
        timestep: paddle.Tensor = None,
        joint_attention_kwargs: Optional[Dict[str, Any]] = None,
        return_dict: bool = True,
    ) -> Union[paddle.Tensor, Transformer2DModelOutput]:
        """
        The [`SD3Transformer2DModel`] forward method.
        Args:
            hidden_states (`paddle.Tensor` of shape `(batch size, channel, height, width)`):
                Input `hidden_states`.
            encoder_hidden_states (`paddle.Tensor` of shape `(batch size, sequence_len, embed_dims)`):
                Conditional embeddings (embeddings computed from the input conditions such as prompts) to use.
            pooled_projections (`paddle.Tensor` of shape `(batch_size, projection_dim)`): Embeddings projected
                from the embeddings of input conditions.
            timestep ( `paddle.Tensor`):
                Used to indicate denoising step.
            joint_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the `AttentionProcessor` as defined under
                `self.processor` in
                [diffusers.models.attention_processor](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/attention_processor.py).
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~models.transformer_2d.Transformer2DModelOutput`] instead of a plain
                tuple.
        Returns:
            If `return_dict` is True, an [`~models.transformer_2d.Transformer2DModelOutput`] is returned, otherwise a
            `tuple` where the first element is the sample tensor.
        """

        # print(str(self))
        # with open("/cwb/wenbin/PaddleMIX/ppdiffusers/examples/inference/AibinSD3/state_dict_817.txt", "a") as time_file:
        #     time_file.write(str(self.state_dict().keys()))

        if joint_attention_kwargs is not None:
            joint_attention_kwargs = joint_attention_kwargs.copy()
            lora_scale = joint_attention_kwargs.pop("scale", 1.0)
        else:
            lora_scale = 1.0

        if USE_PEFT_BACKEND:
            # weight the lora layers by setting `lora_scale` for each PEFT layer
            scale_lora_layers(self, lora_scale)
        else:
            logger.info("Passing `scale` via `joint_attention_kwargs` when not using the PEFT backend is ineffective.")

        height, width = hidden_states.shape[-2:]

        hidden_states = self.pos_embed(hidden_states)  # takes care of adding positional embeddings too.
        temb = self.time_text_embed(timestep, pooled_projections)
        encoder_hidden_states = self.context_embedder(encoder_hidden_states)

        # hidden_states_my = paddle.clone(hidden_states)
        # encoder_hidden_states_my = paddle.clone(encoder_hidden_states)

        # for block in self.transformer_blocks:
        #     if self.training and self.gradient_checkpointing and not use_old_recompute() and False:
        #         pass
        #     else:
        #         encoder_hidden_states_my, hidden_states_my = block(
        #             hidden_states=hidden_states_my, encoder_hidden_states=encoder_hidden_states_my, temb=temb
        #         )

        # encoder_hidden_states_my, hidden_states_my = self.simplified_sd3(
        #     hidden_states=hidden_states_my, encoder_hidden_states=encoder_hidden_states_my, temb=temb
        # )

        # for name, param in self.simplified_sd3.named_parameters():
        #     if param.requires_grad:
        #         print(f"Layer: {name} | Shape: {param.shape} | Values: {param.data.numpy()[:5]}...")
        #     paddle.device.synchronize()

        # paddle.device.synchronize()
        # import nvtx

        # transformer_nvtx = nvtx.start_range(message="simple_transformer", color="yellow")

        hidden_states = self.simplified_sd3(
            hidden_states=hidden_states, encoder_hidden_states=encoder_hidden_states, temb=temb
        )

        # paddle.device.synchronize()
        # nvtx.end_range(transformer_nvtx)

        encoder_hidden_states = None

        # print((hidden_states - hidden_states_my))
        # print(paddle.max(paddle.abs(hidden_states - hidden_states_my)))
        # exit()

        # hidden_states = self.sd3_transformer(hidden_states=hidden_states, encoder_hidden_states=encoder_hidden_states, temb=temb)
        # print(encoder_hidden_states)
        # encoder_hidden_states = None
        # print((hidden_states - hidden_states_my))
        # print(paddle.max(paddle.abs(hidden_states - hidden_states_my)))
        # exit()

        hidden_states = self.norm_out(hidden_states, temb)
        hidden_states = self.proj_out(hidden_states)

        # unpatchify
        patch_size = self.config.patch_size
        height = height // patch_size
        width = width // patch_size

        hidden_states = hidden_states.reshape(
            shape=(hidden_states.shape[0], height, width, patch_size, patch_size, self.out_channels)
        )
        hidden_states = paddle.einsum("nhwpqc->nchpwq", hidden_states)
        output = hidden_states.reshape(
            shape=(hidden_states.shape[0], self.out_channels, height * patch_size, width * patch_size)
        )

        if USE_PEFT_BACKEND:
            # remove `lora_scale` from each PEFT layer
            unscale_lora_layers(self, lora_scale)

        if not return_dict:
            return (output,)

        return Transformer2DModelOutput(sample=output)

    @classmethod
    def custom_modify_weight(cls, state_dict):
        # state_dict["simplified_sd3.linear1.weight"] = paddle.assign(state_dict["transformer_blocks.0.norm1.linear.weight"])
        # state_dict["simplified_sd3.linear1.bias"] = paddle.assign(state_dict["transformer_blocks.0.norm1.linear.bias"])
        for i in range(24):
            base_map_sd3 = [
                (f"linear1.{i}.weight", f"{i}.norm1.linear.weight"),
                (f"linear1.{i}.bias", f"{i}.norm1.linear.bias"),
                (f"q.{i}.weight", f"{i}.attn.to_q.weight"),
                (f"q.{i}.bias", f"{i}.attn.to_q.bias"),
                (f"k.{i}.weight", f"{i}.attn.to_k.weight"),
                (f"k.{i}.bias", f"{i}.attn.to_k.bias"),
                (f"v.{i}.weight", f"{i}.attn.to_v.weight"),
                (f"v.{i}.bias", f"{i}.attn.to_v.bias"),
                (f"ek.{i}.weight", f"{i}.attn.add_k_proj.weight"),
                (f"ek.{i}.bias", f"{i}.attn.add_k_proj.bias"),
                (f"ev.{i}.weight", f"{i}.attn.add_v_proj.weight"),
                (f"ev.{i}.bias", f"{i}.attn.add_v_proj.bias"),
                (f"eq.{i}.weight", f"{i}.attn.add_q_proj.weight"),
                (f"eq.{i}.bias", f"{i}.attn.add_q_proj.bias"),
                (f"to_out_linear.{i}.weight", f"{i}.attn.to_out.0.weight"),
                (f"to_out_linear.{i}.bias", f"{i}.attn.to_out.0.bias"),
                (f"ffn1.{i}.weight", f"{i}.ff.net.0.proj.weight"),
                (f"ffn1.{i}.bias", f"{i}.ff.net.0.proj.bias"),
                (f"ffn2.{i}.weight", f"{i}.ff.net.2.weight"),
                (f"ffn2.{i}.bias", f"{i}.ff.net.2.bias"),
            ]
            if i < 23:
                extra_map_sd3 = [
                    (f"linear_context01.{i}.weight", f"{i}.norm1_context.linear.weight"),
                    (f"linear_context01.{i}.bias", f"{i}.norm1_context.linear.bias"),
                    (f"to_add_out_linear.{i}.weight", f"{i}.attn.to_add_out.weight"),
                    (f"to_add_out_linear.{i}.bias", f"{i}.attn.to_add_out.bias"),
                    (f"ffn_context1.{i}.weight", f"{i}.ff_context.net.0.proj.weight"),
                    (f"ffn_context1.{i}.bias", f"{i}.ff_context.net.0.proj.bias"),
                    (f"ffn_context2.{i}.weight", f"{i}.ff_context.net.2.weight"),
                    (f"ffn_context2.{i}.bias", f"{i}.ff_context.net.2.bias"),
                ]
            else:
                extra_map_sd3 = [
                    ("linear_context0.weight", f"{i}.norm1_context.linear.weight"),
                    ("linear_context0.bias", f"{i}.norm1_context.linear.bias"),
                ]
            map_sd3 = base_map_sd3 + extra_map_sd3

            for to_, from_ in map_sd3:
                if "transformer_blocks." + from_ in state_dict:
                    state_dict["simplified_sd3." + to_] = paddle.assign(state_dict["transformer_blocks." + from_])
                else:
                    print(f"Warning!!: '{from_}' not found in state_dict")

            # if  i > 0:
            #     state_dict["simplified_sd3.linear1.weight"] = paddle.concat([state_dict["simplified_sd3.linear1.weight"], state_dict[f"transformer_blocks.{i}.norm1.linear.weight"]], axis=1)
            #     state_dict["simplified_sd3.linear1.bias"] = paddle.concat([state_dict["simplified_sd3.linear1.bias"],state_dict[f"transformer_blocks.{i}.norm1.linear.bias"]], axis=0)
            #     print("old_weight",state_dict[f"simplified_sd3.linear1.{i}.weight"])
            #     print("old_bias",state_dict[f"simplified_sd3.linear1.{i}.bias"])

            # print("weight",state_dict["simplified_sd3.linear1.weight"])
        # exit(0)