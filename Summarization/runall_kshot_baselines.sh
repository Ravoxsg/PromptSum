### dataset
dataset="billsum" # in ["ccdv/cnn_dailymail", "xsum", "billsum", "samsum"]
k_shot="100" # in ["1", "10", "100"]
device=1
cache='../../hf_models/pegasus-large/'

### backbone model
##### PEGASUS backbone (015_n_400k / 016 / 019)
pretrain_ckpt="../pretrained_ckpt/019/bestckpt_full_model"
pretrain_prompt_ckpt="../pretrained_ckpt/019/bestckpt_prompt"


############################ Baseline v1: Fine-tuning

##### train & val
#echo "start k-shot baseline-1: all-params finetune summary"
#CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model T5Finetune --dataset_name $dataset --few_shot $k_shot --finetune_summary --use_pretrain_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 60 --model_name google/t5-v1_1-large --use_lm_adapted 0 --cache_path $cache --eval_epoch_0
##### test
#echo "start k-shot baseline-1: all-params finetune summary - TEST SET"
#CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model T5Finetune --dataset_name $dataset --full_testset --few_shot $k_shot --finetune_summary --use_pretrain_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 0 --model_name google/t5-v1_1-large --use_lm_adapted 0 --cache_path $cache --valid_size_per_gpu_summary 8 --repetition_penalty 2.5

############################ Baseline v2: Soft prompt tuning

##### train & val
#echo "start k-shot baseline-2: simple prompt-tune summary"
#CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model PegasusSoftPrompt --dataset_name $dataset --few_shot $k_shot --finetune_summary --use_pretrain_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 60 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache --eval_epoch_0 --prompt_number 100
##### test
#echo "start k-shot baseline-2: simple prompt-tune summary - TEST SET"
#CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model PegasusSoftPrompt --dataset_name $dataset --full_testset --few_shot $k_shot --finetune_summary --use_pretrain_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 0 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache --prompt_number 100

############################ Baseline v3: Soft prompt tuning from our pre-trained checkpoint

##### train & val
#echo "start k-shot baseline-3: simple prompt-tune summary with pretrained ckpt"
#CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model PegasusSoftPrompt --dataset_name $dataset --few_shot $k_shot --finetune_summary --pretrain_ckpt $pretrain_ckpt --pretrain_prompt_ckpt $pretrain_prompt_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 60 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache --eval_epoch_0 --prompt_number 100
##### test
echo "start k-shot baseline-3: simple prompt-tune summary with pretrained ckpt - TEST SET"
CUDA_VISIBLE_DEVICES=$device python main.py --model PegasusSoftPrompt --dataset_name $dataset --full_testset --few_shot $k_shot --finetune_summary --pretrain_ckpt $pretrain_ckpt --pretrain_prompt_ckpt $pretrain_prompt_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 0 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache --prompt_number 100

############################ Baseline v4: Soft prompt tuning with TUNE WEIGHTS

# ##### train & val
# echo "start k-shot baseline-4: simple prompt-tune summary TUNE WEIGHTS"
# CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model PegasusSoftPrompt --dataset_name $dataset --few_shot $k_shot --finetune_summary --use_pretrain_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 60 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache --eval_epoch_0 --tune_weights
# ##### test
#echo "start k-shot baseline-4: simple prompt-tune summary TUNE WEIGHTS - TEST SET"
#CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model PegasusSoftPrompt --dataset_name $dataset --full_testset --few_shot $k_shot --finetune_summary --use_pretrain_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 0 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache --tune_weights

############################ Baseline v5: Soft prompt tuning from our pre-trained checkpoint TUNE WEIGHTS

##### train & val
#echo "start k-shot baseline-5: simple prompt-tune summary with pretrained ckpt TUNE WEIGHTS"
#CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model PegasusSoftPrompt --dataset_name $dataset --few_shot $k_shot --finetune_summary --pretrain_ckpt $pretrain_ckpt --pretrain_prompt_ckpt $pretrain_prompt_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 60 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache --eval_epoch_0 --tune_weights
##### test
#echo "start k-shot baseline-5: simple prompt-tune summary with pretrained ckpt TUNE WEIGHTS - TEST SET"
#CUDA_VISIBLE_DEVICES=$device python main_few_shot.py --model PegasusSoftPrompt --dataset_name $dataset --full_testset --few_shot $k_shot --finetune_summary --pretrain_ckpt $pretrain_ckpt --pretrain_prompt_ckpt $pretrain_prompt_ckpt --infer_val_entities --use_entity_chain --use_t5_tagger --if_spacy --max_epoch_summary 0 --model_name google/pegasus-large --use_lm_adapted 0 --cache_path $cache --tune_weights
