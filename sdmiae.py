import argparse
import json
import os
import sys
import numpy as np
import pandas as pd
from tqdm import tqdm
from copy import deepcopy
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms as trn
from torchvision.transforms.functional import to_pil_image
from diffusers import DDIMScheduler, StableDiffusionPipeline
from transformers import ResNetForImageClassification

# Load class names for the dataset
with open('in100_class_index.json', 'r') as f:
    in100_class_index = json.load(f)

class CustomEmbedding(nn.Module):
    """Custom Embedding Layer for handling token embedding updates."""
    def __init__(self, embedding_weights, update_index):
        super().__init__()
        self.zero_index = torch.LongTensor([0]).to(embedding_weights.device)
        self.weights = nn.ModuleList(
            [nn.Embedding.from_pretrained(embedding_weights[i: i + 1], freeze=i != update_index)
             for i in range(embedding_weights.shape[0])]
        )

    def forward(self, x):
        return torch.cat([self.weights[xx.item()](self.zero_index) for xx in x[0]])

def sdmiae_attack(model, images, labels, epsilon=0.03, num_iter=30, mu=0.0, use_epsilon=True):
    """
    SDMIAE (Stable Diffusion Momentum Iterative Adversarial Example) Attack
    Arguments:
    - model: Classifier model used for adversarial attack.
    - images: Input images generated by Stable Diffusion.
    - labels: True labels for the images.
    - epsilon: Perturbation limit (magnitude of attack).
    - num_iter: Number of iterations for the attack.
    - mu: Momentum factor for the iterative attack.
    - use_epsilon: Boolean flag to determine if epsilon constraint is applied.
    """
    images = images.clone().detach().requires_grad_(True)
    original_images = images.clone().detach()  # Store original images for epsilon constraint
    momentum = torch.zeros_like(images)  # Initialize momentum
    alpha = epsilon / num_iter  # Step size for each iteration

    for t in range(num_iter):
        # Forward pass through the classifier to get logits
        outputs = model(images)
        logits = outputs.logits

        # Compute Cross-Entropy Loss
        loss = nn.CrossEntropyLoss()(logits, labels)
        loss.backward()

        # Normalize the gradient
        gradient = images.grad.data
        gradient /= torch.norm(gradient, p=1) + 1e-10  # Normalize gradient to prevent large updates

        # Accumulate momentum: Combines previous gradient with current
        momentum = mu * momentum + gradient
        
        # Apply perturbation based on epsilon constraint
        if use_epsilon:
            # Limit perturbation by epsilon
            images = images + alpha * torch.sign(momentum)
            images = torch.max(torch.min(images, original_images + epsilon), original_images - epsilon).detach_()
        else:
            # No epsilon constraint
            images = images + alpha * torch.sign(momentum)

        # Ensure pixel values stay in valid range [0, 1]
        images = torch.clamp(images, 0, 1).detach_()
        images.requires_grad_(True)  # Retain gradient for next iteration
        model.zero_grad()

    return images

def forward_diffusion(pipe, latents, all_embeddings, num_inference_steps=50, guidance_scale=7.5, eta=0.0):
    """
    Forward pass through Stable Diffusion model to generate images.
    Arguments:
    - pipe: Stable Diffusion pipeline.
    - latents: Latent tensor representing image embeddings.
    - all_embeddings: Text and unconditioned embeddings for diffusion guidance.
    - num_inference_steps: Number of diffusion steps.
    - guidance_scale: Controls the strength of conditioning.
    - eta: Noise scheduling factor.
    """
    pipe.scheduler.set_timesteps(num_inference_steps)
    timesteps_tensor = pipe.scheduler.timesteps.to(pipe.device)
    extra_step_kwargs = pipe.prepare_extra_step_kwargs(None, eta)

    # Diffusion process through the timesteps
    for i, t in tqdm(enumerate(timesteps_tensor), total=len(timesteps_tensor), leave=False):
        latent_model_input = torch.cat([latents] * 2)
        latent_model_input = pipe.scheduler.scale_model_input(latent_model_input, t)
        noise_pred = pipe.unet(latent_model_input, t, encoder_hidden_states=all_embeddings, return_dict=False)[0]
        noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
        latents = pipe.scheduler.step(noise_pred, t, latents, **extra_step_kwargs, return_dict=False)[0]

    latents = latents / pipe.vae.config.scaling_factor
    image = pipe.vae.decode(latents, return_dict=False)[0]

    return pipe.image_processor.postprocess(image, output_type="pt")  # Return as torch tensor

def forward_classifier(x, preprocessor, clf):
    """
    Forward pass through the classifier to obtain logits.
    Arguments:
    - x: Input image tensor.
    - preprocessor: Preprocessing function for classifier input.
    - clf: Classifier model.
    """
    inputs = preprocessor(x)
    return clf(inputs).logits

def main(args):
    """
    Main function to run the SDMIAE attack and image generation process.
    """
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # Load Stable Diffusion pipeline
    pipe = StableDiffusionPipeline.from_pretrained("bguisard/stable-diffusion-nano-2-1", torch_dtype=torch.float32)
    pipe.scheduler = DDIMScheduler.from_config(pipe.scheduler.config)
    pipe.to(device)

    # Load pre-trained classifier for adversarial attack
    preprocessor = trn.Compose([trn.Resize((224, 224), antialias=True),
                                trn.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
    clf = ResNetForImageClassification.from_pretrained("microsoft/resnet-50")
    clf.to(device)
    clf.eval()

    # Process each class ID
    for i in tqdm(range(args.class_ids[0], args.class_ids[1] + 1), desc="Classes"):
        label = in100_class_index[i]
        class_id = torch.tensor([in100_class_index[label][0]], device=device)

        # Generate text embeddings
        text_input = pipe.tokenizer([label], return_tensors="pt", padding="max_length", truncation=True)
        text_input_ids = text_input.input_ids.to(device)
        text_embeddings = pipe.text_encoder(text_input_ids)[0]
        uncond_embeddings = pipe.text_encoder(pipe.tokenizer([""], return_tensors="pt").input_ids.to(device))[0]

        # Initialize random latents
        latents = torch.randn((1, pipe.unet.config.in_channels, args.img_size // pipe.vae_scale_factor,
                               args.img_size // pipe.vae_scale_factor), device=device)
        latents = latents * pipe.scheduler.init_noise_sigma

        # Process each sample
        for j in tqdm(range(args.num_samples_per_class), desc="Samples", leave=False):
            # Generate images using Stable Diffusion
            image = forward_diffusion(pipe, latents, torch.cat([uncond_embeddings, text_embeddings]),
                                      num_inference_steps=args.num_inference_steps,
                                      guidance_scale=args.guidance_scale, eta=0.0)

            # Apply SDMIAE adversarial attack on the generated image
            image = sdmiae_attack(clf, image, class_id, epsilon=args.epsilon, mu=args.mu)

            # Save the resulting adversarial image
            save_dir = os.path.join(f"results/{label}")
            os.makedirs(save_dir, exist_ok=True)
            to_pil_image(image[0].cpu()).save(os.path.join(save_dir, f"sample_{j:02d}.png"))

if __name__ == "__main__":
    # Argument parser for user-defined input parameters
    parser = argparse.ArgumentParser()
    parser.add_argument("--img_size", type=int, default=128, help="Image size for diffusion model.")
    parser.add_argument("--guidance_scale", type=float, default=9.5, help="Guidance scale for stable diffusion.")
    parser.add_argument("--num_inference_steps", type=int, default=20, help="Number of inference steps in diffusion.")
    parser.add_argument("--num_samples_per_class", type=int, default=10, help="Number of samples per class.")
    parser.add_argument("--class_ids", type=int, nargs="+", default=[0, 10], help="Range of class IDs to process.")
    parser.add_argument("--epsilon", type=float, default=0.03, help="Epsilon value for adversarial attack.")
    parser.add_argument("--mu", type=float, default=0.0, help="Momentum parameter for SDMIAE attack.")
    args = parser.parse_args()

    main(args)
