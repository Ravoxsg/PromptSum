### dataset
dataset="xsum"
k_shot="100"
device="7"


### backbone model
##### T5-large backbone
#pretrain_ckpt="/data/qin/PromptSumm/Summarization/t5_tagger_pretrained_ckpt_0520bak/bestckpt_full_model_114k"
#pretrain_prompt_ckpt="/data/qin/PromptSumm/Summarization/t5_tagger_pretrained_ckpt_0520bak/bestckpt_prompt_114k"
#pretrain_ckpt="/data/hailin/PromptSumm/t5_tagger_pretrained_ckpt/012_c_210k/bestckpt_full_model"
#pretrain_prompt_ckpt="/data/hailin/PromptSumm/t5_tagger_pretrained_ckpt/012_c_210k/bestckpt_prompt"
#pretrain_ckpt="/data/hailin/PromptSumm/t5_tagger_pretrained_ckpt/012_cc_ent_v2_120k/012_cc_ent_v2_120k/bestckpt_full_model"
#pretrain_prompt_ckpt="/data/hailin/PromptSumm/t5_tagger_pretrained_ckpt/012_cc_ent_v2_120k/012_cc_ent_v2_120k/bestckpt_prompt"
##### PEGASUS backbone
pretrain_ckpt="/data/hailin/PromptSumm/t5_tagger_pretrained_ckpt/014_c_1070k/bestckpt_full_model"
pretrain_prompt_ckpt="/data/hailin/PromptSumm/t5_tagger_pretrained_ckpt/014_c_1070k/bestckpt_prompt"
cache='/data/ruochen/hf_models/pegasus-large/'
# pretrain_ckpt="/home/ruochen/PromptSumm/t5_tagger_pretrained_ckpt/014_c_1070k/bestckpt_full_model"
# pretrain_prompt_ckpt="/home/ruochen/PromptSumm/t5_tagger_pretrained_ckpt/014_c_1070k/bestckpt_prompt"
# cache='/home/ruochen/hf_models/pegasus-large/'


### k-shot

##### train & val
echo "start k-shot prompt-tune_summary ORACLE"
CUDA_VISIBLE_DEVICES=$device python main.py --use_t5_tagger --model PegasusMixPrompt --dataset_name $dataset --few_shot $k_shot --finetune_summary --guidance_mode target --pretrain_ckpt $pretrain_ckpt --pretrain_prompt_ckpt $pretrain_prompt_ckpt --max_epoch_summary 60 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache

##### test
echo "start k-shot prompt-tune_summary ORACLE - TEST SET"
CUDA_VISIBLE_DEVICES=$device python main.py --use_t5_tagger --model PegasusMixPrompt --dataset_name $dataset --full_testset --few_shot $k_shot --finetune_summary --guidance_mode target --pretrain_ckpt $pretrain_ckpt --pretrain_prompt_ckpt $pretrain_prompt_ckpt --max_epoch_summary 0 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache