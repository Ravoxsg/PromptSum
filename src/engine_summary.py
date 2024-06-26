import gc
import spacy
import time
import logging
import torch.optim as optim
from datasets import load_metric
from rouge_score import rouge_scorer
from bert_score import score
from nltk.tokenize import word_tokenize, sent_tokenize
from tqdm import tqdm
from transformers.optimization import Adafactor
from transformers import T5Config, T5Tokenizer, T5ForConditionalGeneration
from torch.cuda.amp import autocast as autocast
from torch.utils import data
from torch.utils.data import (
    SequentialSampler, RandomSampler
)
from fairscale.optim.oss import OSS
from fairscale.nn.data_parallel import ShardedDataParallel as ShardedDDP
from fairscale.optim.grad_scaler import ShardedGradScaler
gc.enable()

from utils import *
from models.model_summary_soft import *
from dataset.dataset_summary import *


logger = logging.getLogger('engine_summary')

def train(tokenizer, model, train_dataset, valid_dataset, logger, args):
    # total step
    step_tot = (len(train_dataset) // args.gradient_accumulation_steps_summary // args.batch_size_per_gpu_summary // args.n_gpu) * args.max_epoch_summary
    train_sampler = data.distributed.DistributedSampler(train_dataset) if args.local_rank != -1 else data.RandomSampler(train_dataset)
    valid_sampler = SequentialSampler(valid_dataset)

    train_dataloader = get_dataloader(tokenizer, args.num_workers_summary, train_dataset, args.batch_size_per_gpu_summary, args.max_length,
                                      args.max_guidance_length, train_dataset.tokenizer.pad_token_id, train_sampler, args)
    valid_dataloader = get_dataloader(tokenizer, args.num_workers_summary, valid_dataset, args.valid_size_per_gpu_summary, args.max_length,
                                      args.max_guidance_length, valid_dataset.tokenizer.pad_token_id, valid_sampler, args)
    if args.full_testset:
        test_sampler = SequentialSampler(args.test_dataset)
        test_dataloader = get_dataloader(tokenizer, args.num_workers_summary, args.test_dataset, args.valid_size_per_gpu_summary, args.max_length,
                                      args.max_guidance_length, args.test_dataset.tokenizer.pad_token_id, test_sampler, args)
    
    optimizer, scheduler, scaler = None, None, None
    base_optimizer_arguments = {
        "lr": args.lr_summary,
        "clip_threshold": args.max_grad_norm_summary,
        "decay_rate": -0.8,
        "weight_decay": args.weight_decay_summary,
        "scale_parameter": False,
        "relative_step": False
    }
    optimizer = Adafactor
    if args.n_gpu > 1: # distributed training
        optimizer = OSS(params=filter(lambda p: p.requires_grad, model.parameters()), optim=optimizer,
                        **base_optimizer_arguments)
        # distributed training
        model = ShardedDDP(model, optimizer)
    else:
        optimizer = optimizer(params=filter(lambda p: p.requires_grad, model.parameters()), **base_optimizer_arguments)
    model.train()

    logger.info("Begin train...")
    logger.info(f"We will train model in {step_tot} steps")

    result_dict = {
        'epoch': [],
        'val_mean_rouge': [],
        "best_val_mean_rouge": 0.0,
        "val_rouge1": 0.0,
        "val_rouge2": 0.0,
        "val_rougeL": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "BERTScore": 0.0,
        "mean_rs": []
    }

    global_step = 0
    
    if args.eval_epoch_0:
        logger.info("Evaluating (Epoch 0)...")
        dooneeval(model, valid_dataloader, scaler, result_dict, logger, 0, args)

    for i in range(args.max_epoch_summary):
        logger.info(f"Epoch {i+1} / {args.max_epoch_summary}")
        model.train()
        result_dict['epoch'] = i
        allloss, ents = [], []

        for step, batch in enumerate(train_dataloader):
            inputs = {"input_ids": batch[0].to(args.device), "attention_mask": batch[1].to(args.device),
                      "target_ids": batch[2].to(args.device), "target_mask": batch[3].to(args.device),
                      "ents_ids": batch[4].to(args.device), "ents_mask": batch[5].to(args.device)}
            for k in range(inputs["ents_ids"].shape[0]):
                for l in range(inputs["ents_ids"].shape[1]):
                    ent = inputs["ents_ids"][k,l].item()
                    ents.append(ent)
            if scaler is not None:
                with autocast():
                    loss = model(inputs)
            else:
                loss = model(inputs)
            if scaler is not None:
                scaler.scale(loss).backward()
            else:
                loss.backward()
            allloss.append(loss.item())

            if step % args.gradient_accumulation_steps_summary == 0 or step == len(train_dataloader) - 1:
                if scaler is not None:
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    optimizer.step()
                    ents = []
                if scheduler != None:
                    scheduler.step()
                optimizer.zero_grad()
                global_step += 1

                if (args.local_rank in [0, -1]) and (global_step % args.log_step_finetune == 0):
                    logger.info(f"Step: {global_step}, loss: {np.average(allloss):.6f}")

                if args.few_shot == "full":
                    if (args.local_rank in [0, -1]) and (global_step % args.eval_step_summary == 0):
                        print(f"Evaluating (within epoch), step {global_step}...")
                        dooneeval(model, valid_dataloader, scaler, result_dict, logger, i, args)
                        model.train()

        logger.info("finish one epoch")
        if (args.few_shot != "full") and (args.local_rank in [0, -1]):
            # do after every epoch
            logger.info("Evaluating (after epoch)...")
            dooneeval(model, valid_dataloader, scaler, result_dict, logger, i, args)
            model.train()
    # after everything, do it with test:
    if args.full_testset:
        if not(args.zero_shot):
            if (args.model in ['T5Finetune', 'BartFinetune', 'PegasusFinetune']):
                path = args.model_save_path + 'full_weights'
                if args.guidance_mode == "target":
                    path += "_oracle"
                if args.label_smoothing > 0:
                    path += "_ls"
                model.load_state_dict(torch.load(path))
                logger.info("Loaded the full model weights: {path}")
            else:
                if not(args.no_finetuned_sprompt):
                    path = args.model_save_path + f'bestckpt_{args.prompt_number}'
                    if args.use_pretrain_ckpt:
                        path += "_from_pretrained"
                    if args.guidance_mode == "target":
                        path += "_oracle"
                    if args.counterfactual_removal:
                        path = f'{path}_counterfactual'
                    if args.label_smoothing > 0:
                        path += "_ls"
                    best_val_ckpt = torch.load(path)
                    model.promptnumber = best_val_ckpt["promptnumber"]
                    model.promptembedding = nn.parameter.Parameter(best_val_ckpt["promptembedding"])
                    logger.info("Loaded the model prompt: {path}")
        # no need to save again
        args.save_model = False
        args.log_step_finetune = 100
        # initialize new result_dict to save results
        result_dict = {
            'epoch': [],
            'val_mean_rouge': [],
            "best_val_mean_rouge": 0.0,
            "val_rouge1": 0.0,
            "val_rouge2": 0.0,
            "val_rougeL": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "BERTScore": 0.0,
            "mean_rs": []
        }
        result_dict['epoch'] = args.max_epoch_summary
        dooneeval(model, test_dataloader, scaler, result_dict, logger, args.max_epoch_summary, args)
    torch.cuda.empty_cache()
    del model, optimizer, scheduler, scaler, train_dataloader, valid_dataloader,
    gc.collect()
    
    return result_dict

def get_dataloader(tokenizer, num_workers, dataset, batch_size, max_len, max_guidance_len, pad_id, sampler, args):
    collate_fn = SmartBatchingCollate(
        args = args,
        tokenizer = tokenizer,
        max_length=max_len,
        max_guidance_length=max_guidance_len,
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

def dooneeval(modeltoeval, valid_dataloader, scaler, result_dict, logger, i, args):
    if isinstance(modeltoeval, torch.nn.parallel.DistributedDataParallel):
        model = modeltoeval.module
    else:
        model = modeltoeval
    model.eval()
    logger.info("Do one eval!")
    allysrc, allytrue, allypred = [], [], []
    count = 0
    with torch.no_grad():
        logger.info(len(valid_dataloader))
        for step, batch in tqdm(enumerate(valid_dataloader)):
            if step % args.log_step_finetune == 0:
                logger.info(f"Step: {step}, schedule: {(step / len(valid_dataloader)):.3f}")
            inputs = {"input_ids": batch[0].to(args.device), "attention_mask": batch[1].to(args.device),
                      "target_ids": batch[2].to(args.device), "target_mask": batch[3].to(args.device),
                      "ents_ids": batch[4].to(args.device), "ents_mask": batch[5].to(args.device)}
            if scaler is not None:
                with autocast():
                    sen, target, preds = model._generative_step(inputs)
                    tarres, predres = target, preds
                    allysrc.extend(sen)
                    allytrue.extend(tarres)
                    allypred.extend(predres)
            else:
                sen, target, preds = model._generative_step(inputs)
                tarres, predres = target, preds
                allysrc.extend(sen)
                allytrue.extend(tarres)
                allypred.extend(predres)
            count += batch[0].shape[0]
            if count >= args.max_test_size:
                logger.info("Hit the max test size...")
                break
    scorer = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeLsum"], use_stemmer = args.stemmer)
    mean_rs, r1s, r2s, rls = [], [], [], []
    for j in range(len(allytrue)):
        label = allytrue[j]
        summary = allypred[j]
        if args.highlights:
            label = "\n".join(sent_tokenize(label))
            summary = "\n".join(sent_tokenize(summary))
        rouge_score = scorer.score(label, summary)
        r1 = rouge_score["rouge1"].fmeasure
        r2 = rouge_score["rouge2"].fmeasure
        rl = rouge_score["rougeLsum"].fmeasure
        mean_r = (r1 + r2 + rl) / 3
        mean_rs.append(mean_r)
        r1s.append(r1)
        r2s.append(r2)
        rls.append(rl)
    rouge_score = {
        "rouge1": 100 * np.mean(r1s),
        "rouge2": 100 * np.mean(r2s),
        "rougeLsum": 100 * np.mean(rls)
    }
    logger.info('----Validation Results Summary----')
    logger.info(len(allypred))
    logger.info(rouge_score)
    p, r, f1 = entity_eval(allytrue, allypred)
    bs_p, bs_r, bs_f1 = score(allypred, allytrue, lang='en', verbose=True)
    #bs_f1 = 100 * bs_f1.mean()

    # change accordingly
    mean_rouge = (rouge_score["rouge1"] + rouge_score["rouge2"] + rouge_score["rougeLsum"]) / 3
    result_dict['val_mean_rouge'].append(mean_rouge)
    if result_dict['val_mean_rouge'][-1] > result_dict['best_val_mean_rouge']:
        logger.info(f"Epoch {i}, best epoch was updated! val_mean_rouge: {result_dict['val_mean_rouge'][-1]: >4.5f}")
        result_dict["best_val_mean_rouge"] = result_dict['val_mean_rouge'][-1]
        # also append other rouge scores
        result_dict['val_rouge1'] = rouge_score["rouge1"]
        result_dict['val_rouge2'] = rouge_score["rouge2"]
        result_dict['val_rougeL'] = rouge_score["rougeLsum"]
        
        result_dict['precision'] = p
        result_dict['recall'] = r
        result_dict['f1'] = f1
        result_dict["BERTScore"] = bs_f1.mean()

        result_dict["mean_rs"] = np.array(mean_rs)
        result_dict["r1s"] = np.array(r1s)
        result_dict["r2s"] = np.array(r2s)
        result_dict["rls"] = np.array(rls)
        result_dict["bs"] = np.array(bs_f1)

        if args.save_model:
            if not os.path.exists(args.model_save_path):
                os.mkdir(args.model_save_path)
            model_to_save = model.module if hasattr(model, 'module') else model
            if (args.model in ['T5Finetune', 'BartFinetune', 'PegasusFinetune']):
                path = args.model_save_path + 'full_weights'
                if args.guidance_mode == "target":
                    path += "_oracle"
                if args.label_smoothing > 0:
                    path += "_ls"
                if "016" in args.pretrain_ckpt:
                    path += "_v2"
                if "019" in args.pretrain_ckpt:
                    #path += "_v3"
                    path += "_v4"
                torch.save(model_to_save.state_dict(), path)
                logger.info("Saved the full model weights: {path}")
            else:
                path = args.model_save_path + f'bestckpt_{args.prompt_number}'
                if args.use_pretrain_ckpt:
                    path += "_from_pretrained"
                if args.guidance_mode == "target":
                    path += "_oracle"
                if args.counterfactual_removal:
                    path = f'{path}_counterfactual'
                if args.label_smoothing > 0:
                    path += "_ls"
                ckpt = {
                    "promptnumber": model_to_save.promptnumber,
                    "promptembedding": model_to_save.promptembedding
                }
                torch.save(ckpt, path)
                logger.info(f"Saved the model prompt :{path}")
    # abstractivness
    if args.eval_abstractiveness:
        logger.info("Running new n-grams counts...")
        new_unigrams, new_bigrams, new_trigrams, new_quadrigrams = [], [], [], []
        new_unigrams_target, new_bigrams_target, new_trigrams_target, new_quadrigrams_target = [], [], [], []
        for i in tqdm(range(len(allysrc))):
            text_words = allysrc[i].lower()
            text_words = word_tokenize(text_words)
            text_bigrams = [[text_words[j], text_words[j + 1]] for j in range(len(text_words) - 1)]
            text_trigrams = [[text_words[j], text_words[j + 1], text_words[j + 2]] for j in range(len(text_words) - 2)]
            text_quadrigrams = [[text_words[j], text_words[j + 1], text_words[j + 2], text_words[j + 3]] for j in range(len(text_words) - 3)]

            summary_words = allypred[i].lower()
            summary_words = word_tokenize(summary_words)
            unigrams, bigrams, trigrams, quadrigrams = 0, 0, 0, 0
            for j in range(len(summary_words)):
                if not(summary_words[j] in text_words):
                    unigrams += 1
                if j < len(summary_words) - 1:
                    if not([summary_words[j], summary_words[j + 1]] in text_bigrams):
                        bigrams += 1
                if j < len(summary_words) - 2:
                    if not([summary_words[j], summary_words[j + 1], summary_words[j + 2]] in text_trigrams):
                        trigrams += 1
                if j < len(summary_words) - 3:
                    if not([summary_words[j], summary_words[j + 1], summary_words[j + 2], summary_words[j + 3]] in text_quadrigrams):
                        quadrigrams += 1
            unigrams /= max(1, len(summary_words))
            bigrams /= max(1, len(summary_words)-1)
            trigrams /= max(1, len(summary_words)-2)
            quadrigrams /= max(1, len(summary_words)-3)
            new_unigrams.append(unigrams)
            new_bigrams.append(bigrams)
            new_trigrams.append(trigrams)
            new_quadrigrams.append(quadrigrams)

            target_words = allytrue[i].lower()
            target_words = word_tokenize(target_words)
            unigrams, bigrams, trigrams, quadrigrams = 0, 0, 0, 0
            for j in range(len(target_words)):
                if not(target_words[j] in text_words):
                    unigrams += 1
                if j < len(target_words) - 1:
                    if not([target_words[j], target_words[j + 1]] in text_bigrams):
                        bigrams += 1
                if j < len(target_words) - 2:
                    if not([target_words[j], target_words[j + 1], target_words[j + 2]] in text_trigrams):
                        trigrams += 1
                if j < len(target_words) - 3:
                    if not([target_words[j], target_words[j + 1], target_words[j + 2], target_words[j + 3]] in text_quadrigrams):
                        quadrigrams += 1
            unigrams /= max(1, len(target_words))
            bigrams /= max(1, len(target_words)-1)
            trigrams /= max(1, len(target_words)-2)
            quadrigrams /= max(1, len(target_words)-3)
            new_unigrams_target.append(unigrams)
            new_bigrams_target.append(bigrams)
            new_trigrams_target.append(trigrams)
            new_quadrigrams_target.append(quadrigrams)

        new_unigrams = 100 * np.mean(new_unigrams)
        new_bigrams = 100 * np.mean(new_bigrams)
        new_trigrams = 100 * np.mean(new_trigrams)
        new_quadrigrams = 100 * np.mean(new_quadrigrams)
        logger.info(f"Abstractiveness - MODEL || New unigrams: {new_unigrams:.4f}%, bigrams: {new_bigrams:.4f}%, trigrams: {new_trigrams:.4f}, quadrigrams: {new_quadrigrams:.4f}%")
        result_dict["new_unigrams"] = new_unigrams
        result_dict["new_bigrams"] = new_bigrams
        result_dict["new_trigrams"] = new_trigrams
        result_dict["new_quadrigrams"] = new_quadrigrams

        new_unigrams_target = 100 * np.mean(new_unigrams_target)
        new_bigrams_target = 100 * np.mean(new_bigrams_target)
        new_trigrams_target = 100 * np.mean(new_trigrams_target)
        new_quadrigrams_target = 100 * np.mean(new_quadrigrams_target)
        logger.info(f"Abstractiveness - TARGET || New unigrams: {new_unigrams_target:.4f}%, bigrams: {new_bigrams_target:.4f}%, trigrams: {new_trigrams_target:.4f}, quadrigrams: {new_quadrigrams_target:.4f}%")
        result_dict["new_unigrams_target"] = new_unigrams_target
        result_dict["new_bigrams_target"] = new_bigrams_target
        result_dict["new_trigrams_target"] = new_trigrams_target
        result_dict["new_quadrigrams_target"] = new_quadrigrams_target

    return result_dict

def entity_eval(ytrue, ypred):
    spacy_nlp = spacy.load("en_core_web_sm")
    all_p, all_r, all_f1 = [], [], []
    for i in tqdm(range(len(ytrue))):
        ents_true = spacy_nlp(ytrue[i]).ents
        ents_true = [ent.text for ent in ents_true]
        ents_pred = spacy_nlp(ypred[i]).ents
        ents_pred = [ent.text for ent in ents_pred]
        p, r, f1 = 0, 0, 0
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
    logger.info(f"Entity-level eval, mean precision: {p:.4f}, recall: {r:.4f}, F-1: {f1:.4f}")
    
    return p, r, f1

def doinference(modeltoeval, valid_dataloader, scaler, logger, args):
    if isinstance(modeltoeval, torch.nn.parallel.DistributedDataParallel):
        model = modeltoeval.module
    else:
        model = modeltoeval
    model.eval()

    # load weights
    if (args.model in ['T5Finetune', 'BartFinetune', 'PegasusFinetune']):
        path = args.model_save_path + 'full_weights'
        if args.guidance_mode == "target":
            path += "_oracle"
        model.load_state_dict(torch.load(path))
        logger.info(f"Loaded the full model weights: {path}")
    else:
        path = args.model_save_path + f'bestckpt_{args.prompt_number}'
        if args.use_pretrain_ckpt:
            path += "_from_pretrained"
        if args.guidance_mode == "target":
            path += "_oracle"
        if args.counterfactual_removal:
            path = f'{path}_counterfactual'
        best_val_ckpt = torch.load(path)
        model.promptnumber = best_val_ckpt["promptnumber"]
        model.promptembedding = nn.parameter.Parameter(best_val_ckpt["promptembedding"])
        logger.info(f"Loaded the model prompt: {path}")

    logger.info("Do inference!")
    allysrc, allytrue, allypred = [], [], []
    count = 0
    with torch.no_grad():
        logger.info(len(valid_dataloader))
        for step, batch in tqdm(enumerate(valid_dataloader)):
            if step % args.log_step_finetune == 0:
                logger.info("step: %d, schedule: %.3f" % (step, step / len(valid_dataloader)))
            inputs = {"input_ids": batch[0].to(args.device), "attention_mask": batch[1].to(args.device),
                      "target_ids": batch[2].to(args.device), "target_mask": batch[3].to(args.device),
                      "ents_ids": batch[4].to(args.device), "ents_mask": batch[5].to(args.device)}
            if scaler is not None:
                with autocast():
                    sen, target, preds = model._generative_step(inputs)
                    tarres, predres = target, preds
                    allysrc.extend(sen)
                    allytrue.extend(tarres)
                    allypred.extend(predres)
            else:
                sen, target, preds = model._generative_step(inputs)
                tarres, predres = target, preds
                allysrc.extend(sen)
                allytrue.extend(tarres)
                allypred.extend(predres)
            count += batch[0].shape[0]
            if count >= args.max_test_size:
                logger.info("Hit the max test size...")
                break
    scorer = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeLsum"], use_stemmer=args.stemmer)
    r1s, r2s, rls = [], [], []
    for j in range(len(allytrue)):
        label = allytrue[j]
        summary = allypred[j]
        if args.highlights:
            label = "\n".join(sent_tokenize(label))
            summary = "\n".join(sent_tokenize(summary))
        rouge_score = scorer.score(label, summary)
        r1s.append(rouge_score["rouge1"].fmeasure)
        r2s.append(rouge_score["rouge2"].fmeasure)
        rls.append(rouge_score["rougeLsum"].fmeasure)

    r1 = 100 * np.mean(r1s)
    r2 = 100 * np.mean(r2s)
    rl = 100 * np.mean(rls)
    p, r, f1 = entity_eval(allytrue, allypred)

    logger.info('----Validation Results Summary----')
    logger.info(f"Size of the dataset: {len(allypred)}")
    logger.info(f"R-1: {r1:.4f}")
    logger.info(f"R-2: {r2:.4f}")
    logger.info(f"R-L: {rl:.4f}")
    logger.info(f"Precision: {p:.4f}")
    logger.info(f"Recall: {r:.4f}")
    logger.info(f"F-1: {f1:.4f}")

    # abstractivness
    if args.eval_abstractiveness:
        logger.info("Running new n-grams counts...")
        new_unigrams, new_bigrams, new_trigrams, new_quadrigrams = [], [], [], []
        for i in tqdm(range(len(allysrc))):
            text_words = allysrc[i].lower()
            text_words = word_tokenize(text_words)
            text_bigrams = [[text_words[j], text_words[j + 1]] for j in range(len(text_words) - 1)]
            text_trigrams = [[text_words[j], text_words[j + 1], text_words[j + 2]] for j in range(len(text_words) - 2)]
            text_quadrigrams = [[text_words[j], text_words[j + 1], text_words[j + 2], text_words[j + 3]] for j in range(len(text_words) - 3)]

            summary_words = allypred[i].lower()
            summary_words = word_tokenize(summary_words)
            unigrams, bigrams, trigrams, quadrigrams = 0, 0, 0, 0
            for j in range(len(summary_words)):
                if not (summary_words[j] in text_words):
                    unigrams += 1
                if j < len(summary_words) - 1:
                    if not ([summary_words[j], summary_words[j + 1]] in text_bigrams):
                        bigrams += 1
                if j < len(summary_words) - 2:
                    if not ([summary_words[j], summary_words[j + 1], summary_words[j + 2]] in text_trigrams):
                        trigrams += 1
                if j < len(summary_words) - 3:
                    if not ([summary_words[j], summary_words[j + 1], summary_words[j + 2],
                             summary_words[j + 3]] in text_quadrigrams):
                        quadrigrams += 1
            unigrams /= max(1, len(summary_words))
            bigrams /= max(1, len(summary_words) - 1)
            trigrams /= max(1, len(summary_words) - 2)
            quadrigrams /= max(1, len(summary_words) - 3)
            new_unigrams.append(unigrams)
            new_bigrams.append(bigrams)
            new_trigrams.append(trigrams)
            new_quadrigrams.append(quadrigrams)

        new_unigrams = 100 * np.mean(new_unigrams)
        new_bigrams = 100 * np.mean(new_bigrams)
        new_trigrams = 100 * np.mean(new_trigrams)
        new_quadrigrams = 100 * np.mean(new_quadrigrams)
        logger.info(f"New unigrams: {new_unigrams:.4f}")
        logger.info(f"New bigrams: {new_bigrams:.4f}")
        logger.info(f"New trigrams: {new_trigrams:.4f}")
        logger.info(f"New quadrigrams: {new_quadrigrams:.4f}")

    return allysrc, allytrue, allypred
