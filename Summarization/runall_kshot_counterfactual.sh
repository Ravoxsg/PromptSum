### dataset
dataset="xsum"
k_shot="10"

### backbone model
pretrain_ckpt="../pretrained_ckpt/012_cc_ent_v2_120k/012_cc_ent_v2_120k/bestckpt_full_model"
pretrain_prompt_ckpt="../pretrained_ckpt/012_cc_ent_v2_120k/012_cc_ent_v2_120k/bestckpt_prompt"

echo "start k-shot prompt-tune_summary for cnndm with counterfactual training"
python main.py --dataset $dataset --num_seeds 1 --few_shot $k_shot --finetune_summary --pretrain_ckpt $pretrain_ckpt --pretrain_prompt_ckpt $pretrain_prompt_ckpt --max_epoch_summary 60 --counterfactual_removal True
echo "end k-shot prompt-tune_summary for cnndm with counterfactual training"

echo "start CONTROLLING experiments with counterfactual training"
python controllability.py --dataset $dataset --few_shot $k_shot --pretrain_ckpt $pretrain_ckpt  --pretrain_prompt_ckpt $pretrain_prompt_ckpt --counterfactual_trained --big_testset
echo "end CONTROLLING experiments with counterfactual training"