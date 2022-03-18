import os
import torch
import numpy as np
import random
from torch.utils.data import (
    Dataset, DataLoader,
    SequentialSampler, RandomSampler
)



def seed_everything(args):
    random.seed(args.seed)
    os.environ['PYTHONASSEED'] = str(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True


def save_model(modeltoeval, args, steps):
    if isinstance(modeltoeval, torch.nn.parallel.DistributedDataParallel):
        model = modeltoeval.module
    else:
        model = modeltoeval
    model.eval()
    if not os.path.exists(args.save_path):
            os.mkdir(args.save_path)
    if not os.path.exists(args.save_path + "/" + args.save_dir):
        os.mkdir(args.save_path + "/" + args.save_dir)
    model_to_save = model.module if hasattr(model, 'module') else model
    if args.model == 'T5Prompt':
        ckpt = {
            "prompt_length": model_to_save.prompt_length,
            "prompt_embedding": model_to_save.prompt_embedding
        }
    elif args.model == 'T5MixPrompt':
        ckpt = {
            "prompt_dict": model_to_save.prompt_dict,
            "prompt_fix_dict": model_to_save.prompt_fix_dict
        }
    elif args.model == 'T5Finetune':
        ckpt = {
            't5-base': model_to_save.model.state_dict(),
        }
    print("about to save")
    torch.save(ckpt, os.path.join(args.save_path + "/" + args.save_dir, "ckptofT5_"+str(steps)))
    print("ckpt saved")


