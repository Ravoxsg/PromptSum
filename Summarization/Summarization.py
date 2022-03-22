
import argparse
import gc
import spacy
gc.enable()

import time
import logging

from transformers.optimization import Adafactor
from transformers import T5Tokenizer, T5ForConditionalGeneration, T5Config
from torch.cuda.amp import autocast as autocast
from torch.utils import data
from torch.utils.data import (
    SequentialSampler, RandomSampler
)

from fairscale.optim.oss import OSS
from fairscale.nn.data_parallel import ShardedDataParallel as ShardedDDP
from fairscale.optim.grad_scaler import ShardedGradScaler
import pickle
from model import *
from dataset import *
from utils import *
from datasets import load_metric
from rouge_score import rouge_scorer
from nltk.tokenize import sent_tokenize

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)

def get_dataloader(num_workers,dataset, batch_size, max_len, pad_id, sampler):
    collate_fn = SmartBatchingCollate(
        max_length=max_len,
        pad_token_id=pad_id
    )
    dataloader = DataLoader(
        dataset=dataset,
        batch_size=batch_size,
        sampler=sampler,
        collate_fn=collate_fn,
        drop_last=False,
        num_workers=num_workers,
        pin_memory=True
    )
    return dataloader

def train(args, model, train_dataset,valid_dataset):
    # total step
    step_tot = (len(
        train_dataset) // args.gradient_accumulation_steps // args.batch_size_per_gpu // args.n_gpu) * args.max_epoch
    warmup_steps_total = step_tot * args.warmup_steps
    train_sampler = data.distributed.DistributedSampler(train_dataset) if args.local_rank != -1 else data.RandomSampler(
        train_dataset)
    valid_sampler = SequentialSampler(valid_dataset)

    train_dataloader = get_dataloader(args.num_workers, train_dataset, args.batch_size_per_gpu, args.max_length,
                                      train_dataset.tokenizer.pad_token_id,train_sampler)
    valid_dataloader = get_dataloader(args.num_workers, valid_dataset, args.valid_size_per_gpu, args.max_length,
                                      valid_dataset.tokenizer.pad_token_id,valid_sampler)

    base_optimizer_arguments = {"lr": args.lr, "clip_threshold": args.max_grad_norm, "decay_rate": -0.8,
                                "weight_decay": args.weight_decay,
                                "scale_parameter": False, "relative_step": False}
    optimizer = Adafactor
    optimizer = OSS(params=filter(lambda p: p.requires_grad, model.parameters()), optim=optimizer,
                    **base_optimizer_arguments)
    # distributed training
    model = ShardedDDP(model, optimizer)
    model.train()
    #scaler = ShardedGradScaler()
    scheduler = None
    scaler = None

    startepoch = 0
    Best_F1 = 0.0

    logger.info("Begin train...")
    logger.info("We will train model in %d steps" % step_tot)

    result_dict = {
        'epoch': [],
        'val_mean_rouge': [],
        "best_val_mean_rouge": 0.0,
        "val_rouge1": [],
        "val_rouge2": [],
        "val_rougeL": []
    }
    global_step = 0
    lm_lambda = args.lm_lambda
    kd_lamda = args.kd_lamda
    for i in range(startepoch, startepoch + args.max_epoch):
        thisevalstep = args.eval_step
        logger.info(i)
        model.train()
        result_dict['epoch'] = i
        allloss = []
        alllmloss = []
        allkdloss = []
        for step, batch in enumerate(train_dataloader):
            inputs = {"input_ids": batch[0].to(args.device), "attention_mask": batch[1].to(args.device),
                      "target_ids": batch[2].to(args.device), "target_mask": batch[3].to(args.device)}
            inputs_lm = {"input_ids": batch[4].to(args.device), "attention_mask": batch[5].to(args.device),
                         "target_ids": batch[6].to(args.device), "target_mask": batch[7].to(args.device)}
            if scaler is not None:
                with autocast():
                    loss, kdloss = model(inputs,ifcalpre=True)
                    lmloss = model(inputs_lm,ifcalpre=False) * lm_lambda
            else:
                loss, kdloss = model(inputs,ifcalpre=True)
                lmloss = model(inputs_lm,ifcalpre=False) * lm_lambda
            finalloss = loss + lmloss
            finalloss = finalloss * (1.0 - kd_lamda) + kdloss * kd_lamda
            if scaler is not None:
                scaler.scale(finalloss).backward()
            else:
                finalloss.backward()
            allloss.append(loss.item())
            alllmloss.append(lmloss.item())
            allkdloss.append(kdloss.item())
            #print(step, loss.item(), lmloss.item(), kdloss.item())

            if step % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                if scaler is not None:
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    optimizer.step()
                if scheduler != None:
                    scheduler.step()
                optimizer.zero_grad()
                global_step += 1

                if args.local_rank in [0, -1] and global_step % args.log_step == 0:
                    logger.info("step: %d, shcedule: %.3f, loss: %.6f, lmloss: %.6f, kdloss: %.6f" % (
                        global_step, global_step / step_tot, np.average(allloss), np.average(alllmloss),
                        np.average(allkdloss)))

                if args.local_rank in [0, -1] and global_step % thisevalstep == 0:
                    print("not eval!!!")
                    model.train()

        logger.info("finish one epoch")
        if args.local_rank in [0, -1]:
            if i >= 0:
                dooneeval(model,valid_dataloader,args,result_dict,optimizer,scaler,i)
                model.train()

        if args.train_sample:
            logger.info("sampling...")
            logger.info("sampled")

    torch.cuda.empty_cache()
    del model, optimizer, scheduler, scaler, train_dataloader, valid_dataloader,
    gc.collect()

def dooneeval(modeltoeval,valid_dataloader,args,result_dict,optimizer,scaler,i):
    if isinstance(modeltoeval, torch.nn.parallel.DistributedDataParallel):
        model = modeltoeval.module
    else:
        model = modeltoeval
    model.eval()
    logger.info("Do one eval!")
    allytrue = []
    allypred = []
    with torch.no_grad():
        logger.info(len(valid_dataloader))
        for step, batch in enumerate(valid_dataloader):
            logger.info(step)
            inputs = {"input_ids": batch[0].to(args.device), "attention_mask": batch[1].to(args.device),
                      "target_ids": batch[2].to(args.device), "target_mask": batch[3].to(args.device)}
            if scaler is not None:
                with autocast():
                    sen, target, preds = model._generative_step(inputs)
                    tarres, predres = target, preds
                    allytrue.extend(tarres)
                    allypred.extend(predres)
            else:
                sen, target, preds = model._generative_step(inputs)
                tarres, predres = target, preds
                allytrue.extend(tarres)
                allypred.extend(predres)

    scorer = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeLsum"], use_stemmer = args.stemmer)
    r1s, r2s, rls = [], [], []
    for i in range(len(allytrue)):
        label = allytrue[i]
        summary = allypred[i]
        if args.highlights:
            label = "\n".join(sent_tokenize(label))
            summary = "\n".join(sent_tokenize(summary))
        rouge_score = scorer.score(label, summary)
        r1s.append(rouge_score["rouge1"].fmeasure)
        r2s.append(rouge_score["rouge2"].fmeasure)
        rls.append(rouge_score["rougeLsum"].fmeasure)
    rouge_score = {
        "rouge1": 100 * np.mean(r1s),
        "rouge2": 100 * np.mean(r2s),
        "rougeLsum": 100 * np.mean(rls)
    }
    logger.info('----Validation Results Summary----')
    logger.info(len(allypred))
    logger.info(rouge_score)
    p, r, f1 = entity_eval(allytrue, allypred)

    # result_dict['val_rouge1'].append(rouge_score["rouge1"].mid.fmeasure)
    # change accordingly
    mean_rouge = (rouge_score["rouge1"] + rouge_score["rouge2"] + rouge_score["rougeLsum"]) / 3
    result_dict['val_mean_rouge'].append(mean_rouge)
    if result_dict['val_mean_rouge'][-1] > result_dict['best_val_mean_rouge']:
        logger.info("{} epoch, best epoch was updated! val_mean_rouge: {: >4.5f}".format(i, result_dict['val_mean_rouge'][-1]))
        result_dict["best_val_mean_rouge"] = result_dict['val_mean_rouge'][-1]
        # also append other rouge scores
        result_dict['val_rouge1'] = rouge_score["rouge1"]
        result_dict['val_rouge2'] = rouge_score["rouge2"]
        result_dict['val_rougeL'] = rouge_score["rougeLsum"]
        
        result_dict['precision'] = p
        result_dict['recall'] = r
        result_dict['f1'] = f1

        if args.save_model:
            model_to_save = model.module if hasattr(model, 'module') else model
            ckpt = {
                "promptnumber": model_to_save.promptnumber,
                "promptembedding": model_to_save.promptembedding
            }
            torch.save(ckpt, args.save_model_path)

def test(args, test_dataset):

    test_sampler = SequentialSampler(test_dataset)
    test_dataloader = get_dataloader(args.num_workers, test_dataset, args.test_size_per_gpu, args.max_length,
                                      test_dataset.tokenizer.pad_token_id,test_sampler)

    t5model = T5ForConditionalGeneration.from_pretrained(args.model_name, cache_dir=args.cache_path)
    model = T5forSummarization(args, t5model, test_dataset.tokenizer)
    allckpt = torch.load(args.save_model_path)
    model.promptnumber = allckpt["promptnumber"]
    model.promptembedding = allckpt["promptembedding"]
    logger.info("load finished!")

    model.to(args.device)
    model.eval()
    #scaler = ShardedGradScaler()
    scaler = None
    allytrue = []
    allypred = []

    with torch.no_grad():
        for step, batch in enumerate(test_dataloader):
            inputs = {"input_ids": batch[0].to(args.device), "attention_mask": batch[1].to(args.device),
                      "target_ids": batch[2].to(args.device), "target_mask": batch[3].to(args.device)}
            if scaler is not None:
                with autocast():
                    sen,target,preds = model._generative_step(inputs)
                    tarres, predres = target, preds
                    allytrue.extend(tarres)
                    allypred.extend(predres)
            else:
                sen, target, preds = model._generative_step(inputs)
                tarres, predres = target, preds
                allytrue.extend(tarres)
                allypred.extend(predres)
    rouge = load_metric('rouge')
    rouge_score = rouge.compute(references=allytrue, predictions=allypred)
    logger.info('----Test Results Summary----')
    logger.info(len(allypred))
    logger.info(rouge_score)
    logger.info("test_rouge1: %f", rouge_score["rouge1"].mid.fmeasure)
    logger.info("test_rouge2: %f", rouge_score["rouge2"].mid.fmeasure)
    logger.info("test_rougeL: %f", rouge_score["rougeL"].mid.fmeasure)

def entity_eval(ytrue, ypred):
    spacy_nlp = spacy.load("en_core_web_sm")
    all_p = []
    all_r = []
    all_f1 = []
    for i in tqdm(range(len(ytrue))):
        ents_true = spacy_nlp(ytrue[i]).ents
        ents_true = [ent.text for ent in ents_true]
        ents_pred = spacy_nlp(ypred[i]).ents
        ents_pred = [ent.text for ent in ents_pred]
        p = 0
        r = 0
        f1 = 0
        if len(ents_pred) > 0:
            p = 100 * len([x for x in ents_pred if x in ents_true]) / len(ents_pred)
        else:
            if len(ents_true) == 0:
                p = 100
        if len(ents_true) > 0:
            r = 100 * len([x for x in ents_true if x in ents_pred]) / len(ents_true)
        else:
            if len(ents_pred) == 0:
                r = 100
        if (p + r) > 0:
            f1 = (2 * p * r) / (p + r)
        all_p.append(p)
        all_r.append(r)
        all_f1.append(f1)
    p = np.mean(all_p)
    r = np.mean(all_r)
    f1 = np.mean(all_f1)
    print("\nEntity-level eval, mean precision: {:.4f}, recall: {:.4f}, F-1: {:.4f}".format(p, r, f1))
    
    return p, r, f1

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="latentRE")
    parser.add_argument("--cuda", dest="cuda", type=str,
                        default="0", help="gpu id")

    parser.add_argument("--data_dir", dest="data_dir", type=str,
                        default="/data/mathieu/DATASETS/PromptSumm/")
    parser.add_argument("--dataset", dest="dataset", type=str,
                        default="cnndm")
    parser.add_argument("--few_shot", dest="few_shot", type=int,
                        default=64)

    parser.add_argument("--lr", dest="lr", type=float,
                        default=5e-1, help='learning rate')
    parser.add_argument("--lm_lambda", dest="lm_lambda", type=float,
                        default=0.0, help='language model loss lambda')
    parser.add_argument("--kd_lamda", dest="kd_lamda", type=float,
                        default=0.0, help='kd loss lambda')
    parser.add_argument("--startindex", dest="startindex", type=int,
                        default=0, help="start index")
    parser.add_argument("--taskindex", dest="taskindex", type=int,
                        default=0, help="task index")
    parser.add_argument("--batch_size_per_gpu", dest="batch_size_per_gpu", type=int,
                        default=1, help="batch size per gpu")
    parser.add_argument("--valid_size_per_gpu", dest="valid_size_per_gpu", type=int,
                        default=8, help="valid size per gpu")
    parser.add_argument("--test_size_per_gpu", dest="test_size_per_gpu", type=int,
                        default=8, help="test size per gpu")
    parser.add_argument("--gradient_accumulation_steps", dest="gradient_accumulation_steps", type=int,
                        default=8, help="gradient accumulation steps")
    parser.add_argument("--max_epoch", dest="max_epoch", type=int,
                        default=80, help="max epoch number")
    parser.add_argument("--num_workers", dest="num_workers", type=int,
                        default=0, help="dataloader num_workers")

    parser.add_argument("--save_step", dest="save_step", type=int,
                        default=100000, help="step to save")
    parser.add_argument("--log_step", dest="log_step", type=int,
                        default=1, help="how many steps to log")
    parser.add_argument("--eval_step", dest="eval_step", type=int,
                        default=100000, help="how many steps to eval")

    parser.add_argument("--save_dir", dest="save_dir", type=str,
                        default="t5_ckpt", help="ckpt dir to save")
    parser.add_argument("--tosavepath", dest="tosavepath", type=str,
                        default="t5_sum_ckpt", help="ckpt dir to save")
    parser.add_argument("--seed", dest="seed", type=int,
                        default=42, help="seed for network")


    parser.add_argument("--model", dest="model", type=str,
                        default="T5Summarization", help="{T5NER}")
    parser.add_argument("--model_name", dest="model_name", type=str,
                        default="google/t5-v1_1-large", help="{t5-base,google/t5-v1_1-base}")
    parser.add_argument("--train_file_name", dest="train_file_name", type=str,
                        default="data_conll/newtrain.txt", help="train data file path")
    parser.add_argument("--valid_file_name", dest="valid_file_name", type=str,
                        default="data_conll/newvalid.txt", help="valid data file path")
    parser.add_argument("--test_file_name", dest="test_file_name", type=str,
                        default="data_conll/newtest.txt", help="test data file path")
    parser.add_argument("--train_sample", dest="train_sample",
                        default = True, help="dynamic sample or not")
    parser.add_argument("--max_length", dest="max_length", type=int,
                        default=512, help="max sentence length")

    parser.add_argument("--weight_decay", dest="weight_decay", type=float,
                        default=1e-5, help="weight decay")
    parser.add_argument("--adam_epsilon", dest="adam_epsilon", type=float,
                        default = 1e-8, help="adam epsilon")
    parser.add_argument("--warmup_steps", dest="warmup_steps", type=float,
                        default=0.01, help="warmup steps")
    parser.add_argument("--max_grad_norm", dest="max_grad_norm", type=float,
                        default=1.0, help="max grad norm")

    parser.add_argument("--local_rank", dest="local_rank", type=int,
                        default=-1, help="local rank")

    parser.add_argument("--use_lm_adapted", dest="use_lm_adapted", type=int,
                        default=1, help="whether to use lm_adapted model")
    parser.add_argument("--lm_adapted_path", dest="lm_adapted_path", type=str,
                        default="/data/mathieu/lm_adapted_t5model/torch_ckpt/large/pytorch_model.bin",
                        help="The path of lm_adapted model")
    parser.add_argument("--cache_path", dest="cache_path", type=str,
                        default="/data/mathieu/hf_models/t5-v1-large/",
                        help="The path of huggingface cache")
    parser.add_argument("--prompt_number", dest="prompt_number", type=int,
                        default=300, help="The number of prompt")
    parser.add_argument("--ifckpt_onlymodel", dest="ifckpt_onlymodel", type=int,
                        default=1, help="If ckpt only contains model. Default: True, only contains model")

    parser.add_argument("--save_model", dest="save_model", type=bool,
                        default=False, help="whether to save the model or not")
    parser.add_argument("--save_model_path", dest="save_model_path", type=str,
                        default="", help="the path where to save the model")
    parser.add_argument("--stemmer", type=bool, default=True)
    parser.add_argument("--highlights", type=bool, default=True)

    args = parser.parse_args()

    print(args)

    # set cuda
    os.environ["CUDA_VISIBLE_DEVICES"] = args.cuda
    if args.local_rank == -1:
        device = torch.device("cuda")
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
    args.device = device
    args.n_gpu = len(args.cuda.split(","))
    initialseed = args.seed
    seed_everything(args)

    # log train
    if args.local_rank in [0, -1]:
        if not os.path.exists("./log"):
            os.mkdir("./log")
        with open("./log/trainner_log", 'a+') as f:
            f.write(str(time.ctime()) + "\n")
            f.write(str(args) + "\n")
            f.write("----------------------------------------------------------------------------\n")

    allgentasktokens = ["summerizationcnndm"]
    thistaskname = "cnn daily mail "
    thistaskfold = "cnndm"
    args.taskfold = thistaskfold
    t5model = T5ForConditionalGeneration.from_pretrained(args.model_name, cache_dir=args.cache_path)
    tokenizer = T5Tokenizer.from_pretrained(args.model_name, cache_dir=args.cache_path)
    for gg in range(len(allgentasktokens)):
        gentasktoken = allgentasktokens[gg]
        tokenizer.add_tokens(gentasktoken)
        logger.info('gen token = {} , gen token id = {}'.format(
            gentasktoken, tokenizer.convert_tokens_to_ids(gentasktoken)
        ))
    answertoken = "__ans__"
    special_tokens = {"ans_token": answertoken}
    tokenizer.add_tokens(list(special_tokens.values()))
    special_token_ids = {k: tokenizer.convert_tokens_to_ids(v) for k, v in special_tokens.items()}

    model = T5forSummarization(args, t5model, tokenizer)
    promptnumber = args.prompt_number
    promptembedding = getpromptembedding(model, tokenizer, promptnumber, thistaskname)

    model.set_prompt_embedding(promptnumber, promptembedding)
    model.to(args.device)

    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info("The model has {} trainable parameters".format(n_params))

    thistrainfilename = args.data_dir + args.dataset + "/{}/seed_0/train.txt".format(args.few_shot)
    thisvalidfilename = args.data_dir + args.dataset + "/{}/seed_0/valid.txt".format(args.few_shot)
    print(thistrainfilename, thisvalidfilename)
    args.train_file_name = thistrainfilename
    args.valid_file_name = thisvalidfilename

    train_dataset = T5SummarizationDataset(args.train_file_name, args.max_length, tokenizer, allgentasktokens, answertoken)
    valid_dataset = T5SummarizationDataset(args.valid_file_name, args.max_length, tokenizer, allgentasktokens, answertoken)

    logger.info("Finish prepare model and dataset")
    logger.info("Start training")

    train(args, model, train_dataset, valid_dataset)
    logger.info("Finish training")

    if args.local_rank in [0, -1]:
        logger.info("Start testing")
        logger.info("Testing...")
        test(args, test_dataset)
        logger.info("Finish testing!")

    if args.local_rank != -1:
        torch.distributed.destroy_process_group()









