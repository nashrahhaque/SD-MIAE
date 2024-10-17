# SDMIAE - Stable Diffusion Momentum Iterative Adversarial Example

This repository contains the implementation of the **SDMIAE** attack, which applies momentum-based iterative adversarial perturbations to images generated using the **Stable Diffusion** model. The aim is to generate adversarial examples that can mislead a pre-trained classifier while using Stable Diffusion for image generation.

## **Setup Instructions**

To set up the environment for using SDMIAE, follow these steps:

1. **Create a new Conda environment:**

    ```bash
    conda create -n sdmiae python=3.9
    ```

2. **Activate the environment:**

    ```bash
    conda activate sdmiae
    ```

3. **Install the necessary dependencies:**

    ```bash
    python -m pip install torch torchvision torchaudio  # Version 2.1.0 or later
    python -m pip install xformers diffusers transformers accelerate pandas
    ```

4. **Clone the repository:**

    ```bash
    git clone https://github.com/nashrahaque/sdmiae.git
    cd sdmiae
    ```

## **Running the Code**

Once the environment is set up, you can run the code using the following command:

```bash
python sdmiae.py --img_size 128 --guidance_scale 9.5 --epsilon 0.2 --mu 1.0 --num_inference_steps 20 --num_samples_per_class 10 --class_ids 0 10

## **Results and Acknowledgments**

The generated adversarial examples will be saved in the `results/` directory, organized by class label. Each adversarial image is saved as `sample_{j:02d}.png` within its respective class folder.

This novel framework builds upon https://openreview.net/forum?id=D87rimdkGd by incorporating momentum-based optimization techniques. 

