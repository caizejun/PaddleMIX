# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
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
from typing import Callable, List, Optional, Union

import numpy as np
import paddle
import PIL
from paddlenlp.transformers import XLMRobertaTokenizer
from PIL import Image

from ...models import UNet2DConditionModel, VQModel
from ...schedulers import DDIMScheduler
from ...utils import logging, randn_tensor, replace_example_docstring
from ..pipeline_utils import DiffusionPipeline, ImagePipelineOutput
from .text_encoder import MultilingualCLIP

logger = logging.get_logger(__name__)
EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> from ppdiffusers import KandinskyImg2ImgPipeline, KandinskyPriorPipeline
        >>> from ppdiffusers.utils import load_image
        >>> import paddle

        >>> pipe_prior = KandinskyPriorPipeline.from_pretrained(
        ...     "kandinsky-community/kandinsky-2-1-prior", paddle_dtype=paddle.float16
        ... )

        >>> prompt = "A red cartoon frog, 4k"
        >>> image_emb, zero_image_emb = pipe_prior(prompt, return_dict=False)

        >>> pipe = KandinskyImg2ImgPipeline.from_pretrained(
        ...     "kandinsky-community/kandinsky-2-1", paddle_dtype=paddle.float16
        ... )

        >>> init_image = load_image(
        ...     "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main"
        ...     "/kandinsky/frog.png"
        ... )

        >>> image = pipe(
        ...     prompt,
        ...     image=init_image,
        ...     image_embeds=image_emb,
        ...     negative_image_embeds=zero_image_emb,
        ...     height=768,
        ...     width=768,
        ...     num_inference_steps=100,
        ...     strength=0.2,
        ... ).images

        >>> image[0].save("red_frog.png")
        ```
"""


def get_new_h_w(h, w, scale_factor=8):
    new_h = h // scale_factor**2
    if h % scale_factor**2 != 0:
        new_h += 1
    new_w = w // scale_factor**2
    if w % scale_factor**2 != 0:
        new_w += 1
    return new_h * scale_factor, new_w * scale_factor


def prepare_image(pil_image, w=512, h=512):
    pil_image = pil_image.resize((w, h), resample=Image.BICUBIC, reducing_gap=1)
    arr = np.array(pil_image.convert("RGB"))
    arr = arr.astype(np.float32) / 127.5 - 1
    arr = np.transpose(arr, [2, 0, 1])
    image = paddle.to_tensor(data=arr).unsqueeze(axis=0)
    return image


class KandinskyImg2ImgPipeline(DiffusionPipeline):
    """
    Pipeline for image-to-image generation using Kandinsky

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        text_encoder ([`MultilingualCLIP`]):
            Frozen text-encoder.
        tokenizer ([`XLMRobertaTokenizer`]):
            Tokenizer of class
        scheduler ([`DDIMScheduler`]):
            A scheduler to be used in combination with `unet` to generate image latents.
        unet ([`UNet2DConditionModel`]):
            Conditional U-Net architecture to denoise the image embedding.
        movq ([`VQModel`]):
            MoVQ image encoder and decoder
    """

    def __init__(
        self,
        text_encoder: MultilingualCLIP,
        movq: VQModel,
        tokenizer: XLMRobertaTokenizer,
        unet: UNet2DConditionModel,
        scheduler: DDIMScheduler,
    ):
        super().__init__()
        self.register_modules(
            text_encoder=text_encoder, tokenizer=tokenizer, unet=unet, scheduler=scheduler, movq=movq
        )
        self.movq_scale_factor = 2 ** (len(self.movq.config.block_out_channels) - 1)

    def get_timesteps(self, num_inference_steps, strength):
        # get the original timestep using init_timestep
        init_timestep = min(int(num_inference_steps * strength), num_inference_steps)
        t_start = max(num_inference_steps - init_timestep, 0)
        timesteps = self.scheduler.timesteps[t_start:]
        return timesteps, num_inference_steps - t_start

    def prepare_latents(self, latents, latent_timestep, shape, dtype, generator, scheduler):
        if latents is None:
            latents = randn_tensor(shape, generator=generator, dtype=dtype)
        else:
            if latents.shape != shape:
                raise ValueError(f"Unexpected latents shape, got {latents.shape}, expected {shape}")
        latents = latents * scheduler.init_noise_sigma
        shape = latents.shape
        noise = randn_tensor(shape, generator=generator, dtype=dtype)
        latents = self.add_noise(latents, noise, latent_timestep)
        return latents

    def _encode_prompt(self, prompt, num_images_per_prompt, do_classifier_free_guidance, negative_prompt=None):
        batch_size = len(prompt) if isinstance(prompt, list) else 1
        # get prompt text embeddings
        text_inputs = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=77,
            truncation=True,
            return_attention_mask=True,
            add_special_tokens=True,
            return_tensors="pd",
        )
        text_input_ids = text_inputs.input_ids
        untruncated_ids = self.tokenizer(prompt, padding="longest", return_tensors="pd").input_ids
        if (
            untruncated_ids.shape[-1] >= text_input_ids.shape[-1]
            and not paddle.equal_all(x=text_input_ids, y=untruncated_ids).item()
        ):
            removed_text = self.tokenizer.batch_decode(untruncated_ids[:, self.tokenizer.model_max_length - 1 : -1])
            logger.warning(
                f"The following part of your input was truncated because CLIP can only handle sequences up to {self.tokenizer.model_max_length} tokens: {removed_text}"
            )
        text_mask = text_inputs.attention_mask
        prompt_embeds, text_encoder_hidden_states = self.text_encoder(
            input_ids=text_input_ids, attention_mask=text_mask
        )
        prompt_embeds = prompt_embeds.repeat_interleave(repeats=num_images_per_prompt, axis=0)
        text_encoder_hidden_states = text_encoder_hidden_states.repeat_interleave(
            repeats=num_images_per_prompt, axis=0
        )
        text_mask = text_mask.repeat_interleave(repeats=num_images_per_prompt, axis=0)
        if do_classifier_free_guidance:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [""] * batch_size
            elif type(prompt) is not type(negative_prompt):
                raise TypeError(
                    f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} != {type(prompt)}."
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`: {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches the batch size of `prompt`."
                )
            else:
                uncond_tokens = negative_prompt
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=77,
                truncation=True,
                return_attention_mask=True,
                add_special_tokens=True,
                return_tensors="pd",
            )
            uncond_text_input_ids = uncond_input.input_ids
            uncond_text_mask = uncond_input.attention_mask
            negative_prompt_embeds, uncond_text_encoder_hidden_states = self.text_encoder(
                input_ids=uncond_text_input_ids, attention_mask=uncond_text_mask
            )

            # duplicate unconditional embeddings for each generation per prompt, using mps friendly method

            seq_len = negative_prompt_embeds.shape[1]
            negative_prompt_embeds = negative_prompt_embeds.tile(repeat_times=[1, num_images_per_prompt])
            negative_prompt_embeds = negative_prompt_embeds.reshape([batch_size * num_images_per_prompt, seq_len])
            seq_len = uncond_text_encoder_hidden_states.shape[1]
            uncond_text_encoder_hidden_states = uncond_text_encoder_hidden_states.tile(
                repeat_times=[1, num_images_per_prompt, 1]
            )
            uncond_text_encoder_hidden_states = uncond_text_encoder_hidden_states.reshape(
                [batch_size * num_images_per_prompt, seq_len, -1]
            )
            uncond_text_mask = uncond_text_mask.repeat_interleave(repeats=num_images_per_prompt, axis=0)

            # done duplicates

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            prompt_embeds = paddle.concat(x=[negative_prompt_embeds, prompt_embeds])
            text_encoder_hidden_states = paddle.concat(
                x=[uncond_text_encoder_hidden_states, text_encoder_hidden_states]
            )
            text_mask = paddle.concat(x=[uncond_text_mask, text_mask])
        return prompt_embeds, text_encoder_hidden_states, text_mask

    def add_noise(
        self, original_samples: paddle.Tensor, noise: paddle.Tensor, timesteps: paddle.Tensor
    ) -> paddle.Tensor:
        betas = paddle.linspace(start=0.0001, stop=0.02, num=1000).astype("float32")
        alphas = 1.0 - betas
        alphas_cumprod = paddle.cumprod(dim=0, x=alphas)
        alphas_cumprod = alphas_cumprod.cast(dtype=original_samples.dtype)
        sqrt_alpha_prod = alphas_cumprod[timesteps] ** 0.5
        sqrt_alpha_prod = sqrt_alpha_prod.flatten()
        while len(sqrt_alpha_prod.shape) < len(original_samples.shape):
            sqrt_alpha_prod = sqrt_alpha_prod.unsqueeze(axis=-1)
        sqrt_one_minus_alpha_prod = (1 - alphas_cumprod[timesteps]) ** 0.5
        sqrt_one_minus_alpha_prod = sqrt_one_minus_alpha_prod.flatten()
        while len(sqrt_one_minus_alpha_prod.shape) < len(original_samples.shape):
            sqrt_one_minus_alpha_prod = sqrt_one_minus_alpha_prod.unsqueeze(axis=-1)
        noisy_samples = sqrt_alpha_prod * original_samples + sqrt_one_minus_alpha_prod * noise
        return noisy_samples

    @paddle.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
        self,
        prompt: Union[str, List[str]],
        image: Union[paddle.Tensor, PIL.Image.Image, List[paddle.Tensor], List[PIL.Image.Image]],
        image_embeds: paddle.Tensor,
        negative_image_embeds: paddle.Tensor,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        height: int = 512,
        width: int = 512,
        num_inference_steps: int = 100,
        strength: float = 0.3,
        guidance_scale: float = 7.0,
        num_images_per_prompt: int = 1,
        generator: Optional[Union[paddle.Generator, List[paddle.Generator]]] = None,
        output_type: Optional[str] = "pil",
        callback: Optional[Callable[[int, int, paddle.Tensor], None]] = None,
        callback_steps: int = 1,
        return_dict: bool = True,
    ):
        """
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide the image generation.
            image (`paddle.Tensor`, `PIL.Image.Image`):
                `Image`, or tensor representing an image batch, that will be used as the starting point for the
                process.
            image_embeds (`paddle.Tensor` or `List[paddle.Tensor]`):
                The clip image embeddings for text prompt, that will be used to condition the image generation.
            negative_image_embeds (`paddle.Tensor` or `List[paddle.Tensor]`):
                The clip image embeddings for negative text prompt, will be used to condition the image generation.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. Ignored when not using guidance (i.e., ignored
                if `guidance_scale` is less than `1`).
            height (`int`, *optional*, defaults to 512):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to 512):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 100):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            strength (`float`, *optional*, defaults to 0.3):
                Conceptually, indicates how much to transform the reference `image`. Must be between 0 and 1. `image`
                will be used as a starting point, adding more noise to it the larger the `strength`. The number of
                denoising steps depends on the amount of noise initially added. When `strength` is 1, added noise will
                be maximum and the denoising process will run for the full number of iterations specified in
                `num_inference_steps`. A value of 1, therefore, essentially ignores `image`.
            guidance_scale (`float`, *optional*, defaults to 4.0):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            generator (`paddle.Generator` or `List[paddle.Generator]`, *optional*):
                One or a list of paddle generator(s).
                to make generation deterministic.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between: `"pil"` (`PIL.Image.Image`), `"np"`
                (`np.array`) or `"pt"` (`paddle.Tensor`).
            callback (`Callable`, *optional*):
                A function that calls every `callback_steps` steps during inference. The function is called with the
                following arguments: `callback(step: int, timestep: int, latents: paddle.Tensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function is called. If not specified, the callback is called at
                every step.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.ImagePipelineOutput`] instead of a plain tuple.

        Examples:

        Returns:
            [`~pipelines.ImagePipelineOutput`] or `tuple`
        """
        # 1. Define call parameters
        if isinstance(prompt, str):
            batch_size = 1
        elif isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")
        batch_size = batch_size * num_images_per_prompt
        do_classifier_free_guidance = guidance_scale > 1.0

        # 2. get text and image embeddings
        prompt_embeds, text_encoder_hidden_states, _ = self._encode_prompt(
            prompt, num_images_per_prompt, do_classifier_free_guidance, negative_prompt
        )
        if isinstance(image_embeds, list):
            image_embeds = paddle.concat(x=image_embeds, axis=0)
        if isinstance(negative_image_embeds, list):
            negative_image_embeds = paddle.concat(x=negative_image_embeds, axis=0)
        if do_classifier_free_guidance:
            image_embeds = image_embeds.repeat_interleave(repeats=num_images_per_prompt, axis=0)
            negative_image_embeds = negative_image_embeds.repeat_interleave(repeats=num_images_per_prompt, axis=0)
            image_embeds = paddle.concat(x=[negative_image_embeds, image_embeds], axis=0).cast(
                dtype=prompt_embeds.dtype
            )

        # 3. pre-processing initial image
        if not isinstance(image, list):
            image = [image]
        if not all(isinstance(i, (PIL.Image.Image, paddle.Tensor)) for i in image):
            raise ValueError(
                f"Input is in incorrect format: {[type(i) for i in image]}. Currently, we only support  PIL image and pypaddle tensor"
            )
        image = paddle.concat(x=[prepare_image(i, width, height) for i in image], axis=0)
        image = image.cast(dtype=prompt_embeds.dtype)
        latents = self.movq.encode(image)["latents"]
        latents = latents.repeat_interleave(repeats=num_images_per_prompt, axis=0)

        # 4. set timesteps
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps_tensor, num_inference_steps = self.get_timesteps(num_inference_steps, strength)

        # the formular to calculate timestep for add_noise is taken from the original kandinsky repo
        latent_timestep = int(self.scheduler.config.num_train_timesteps * strength) - 2
        latent_timestep = paddle.to_tensor(data=[latent_timestep] * batch_size, dtype=timesteps_tensor.dtype)
        num_channels_latents = self.unet.config.in_channels
        height, width = get_new_h_w(height, width, self.movq_scale_factor)

        # 5. Create initial latent
        latents = self.prepare_latents(
            latents,
            latent_timestep,
            (batch_size, num_channels_latents, height, width),
            text_encoder_hidden_states.dtype,
            generator,
            self.scheduler,
        )

        # 6. Denoising loop
        for i, t in enumerate(self.progress_bar(timesteps_tensor)):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = paddle.concat(x=[latents] * 2) if do_classifier_free_guidance else latents
            added_cond_kwargs = {"text_embeds": prompt_embeds, "image_embeds": image_embeds}
            noise_pred = self.unet(
                sample=latent_model_input,
                timestep=t,
                encoder_hidden_states=text_encoder_hidden_states,
                added_cond_kwargs=added_cond_kwargs,
                return_dict=False,
            )[0]
            if do_classifier_free_guidance:
                noise_pred, variance_pred = noise_pred.split(noise_pred.shape[1] // latents.shape[1], axis=1)
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(chunks=2)
                _, variance_pred_text = variance_pred.chunk(chunks=2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
                noise_pred = paddle.concat(x=[noise_pred, variance_pred_text], axis=1)
            if not (
                hasattr(self.scheduler.config, "variance_type")
                and self.scheduler.config.variance_type in ["learned", "learned_range"]
            ):
                noise_pred, _ = noise_pred.split(noise_pred.shape[1] // latents.shape[1], axis=1)

            # compute the previous noisy sample x_t -> x_t-1
            latents = self.scheduler.step(noise_pred, t, latents, generator=generator).prev_sample
            if callback is not None and i % callback_steps == 0:
                callback(i, t, latents)

        # 7. post-processing
        image = self.movq.decode(latents, force_not_quantize=True)["sample"]
        if output_type not in ["pd", "np", "pil"]:
            raise ValueError(f"Only the output types `pt`, `pil` and `np` are supported not output_type={output_type}")
        if output_type in ["np", "pil"]:
            image = image * 0.5 + 0.5
            image = image.clip(min=0, max=1)
            image = image.cpu().transpose(perm=[0, 2, 3, 1]).astype(dtype="float32").numpy()
        if output_type == "pil":
            image = self.numpy_to_pil(image)
        if not return_dict:
            return (image,)
        return ImagePipelineOutput(images=image)
