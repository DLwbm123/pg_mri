#!/bin/sh

#SBATCH --ntasks=1
#SBATCH --gres=gpu:2
#SBATCH --cpus-per-task=20
#SBATCH --priority=TOP
#SBATCH --mem=20G
#SBATCH --verbose
#SBATCH --time 7-0:00:00
#SBATCH --job-name=brain

#SBATCH -D /home/tbbakke/mrimpro

echo "Running..."

# This is either ml or fastmri?
source /home/tbbakke/anaconda3/bin/activate fastmri

nvidia-smi

# Do your stuff

CUDA_VISIBLE_DEVICES=0,1 HDF5_USE_FILE_LOCKING=FALSE python -m src.train_RL_model_sweep \
--dataset fastmri --data-path /home/tbbakke/data/fastMRI/brain/ --exp-dir /home/tbbakke/mrimpro/brain_exp_results/ --resolution 256 \
--recon-model-checkpoint /home/tbbakke/fastMRI-shi/models/unet/al_brain_nonorig_highres256_8to4in2/model.pt --recon-model-name nounc \
--of-which-four-pools 0 --num-chans 8 --batch-size 4 --impro-model-name convpool --fc-size 256 --accelerations 8 --acquisition-steps 16 --report-interval 1000 \
--lr 5e-5 --sample-rate 0.2 --seed 0 --num-workers 4 --in-chans 1 --num-epochs 50 --num-pools 5 --pool-stride 1 \
--estimator full_step --num-trajectories 8 --num-dev-trajectories 4 --greedy False --data-range volume --baseline-type selfstep \
--scheduler-type multistep --lr-multi-step-size 10 20 30 40 --lr-gamma .5 --acquisition None --center-volume False --batches-step 4 \
--wandb True --do-train-ssim False --project mrimpro_brain --original_setting False --low_res False --gamma 1.0