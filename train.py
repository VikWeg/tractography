import os
import argparse
import shutil

from tensorflow.keras import optimizers as keras_optimizers
import numpy as np

from multiprocessing import cpu_count

from models import MODELS
from utils import setup_env, timestamp, parse_callbacks

import configs


@setup_env
def train(config):
    
    out_dir = os.path.join("models",
                           config["model_name"],
                           config["model_type"],
                           timestamp())
    os.makedirs(out_dir, exist_ok=True)
    configs.deep_update(config, {"out_dir": out_dir})

    model = MODELS[config["model_name"]](config)

    train_seq = model.get_sequence(config["train_path"], config["batch_size"])
    eval_seq = model.get_sequence(config["eval_path"], config["batch_size"],
        istraining=False)
    configs.deep_update(config, {"train_seq": train_seq, "eval_seq": eval_seq})

    callbacks = parse_callbacks(config["callbacks"])

    optimizer=getattr(keras_optimizers, config["optimizer"])(
        **config["optimizer_params"]
    )
    model.compile(optimizer)

    configs.save(config)

    try:
        print("\nStart training...")

        no_exception = True

        model.keras.fit_generator(
            train_seq,
            callbacks=callbacks,
            validation_data=eval_seq,
            epochs=config["epochs"],
            shuffle=config["shuffle"],
            max_queue_size=4 * config["batch_size"],
            use_multiprocessing=True,
            workers=cpu_count()
        )
    except Exception as e:
        shutil.rmtree(out_dir)
        no_exception = False
        raise e
    finally:
        if no_exception:
            model_path = os.path.join(out_dir, "final_model.h5")
            print("\nSaving {}".format(model_path))
            model.keras.save(model_path)

    return model


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description="Train a fiber tracking model")

    parser.add_argument("config_path", type=str, nargs="?",
        help="Path to model config.")

    parser.add_argument("--model_name", type=str, choices=list(MODELS.keys()),
        help="Name of model to be trained.")

    parser.add_argument("--model_type", type=str,
        choices=["prior", "conditional"],
        help="Specify if model has type conditional or prior.")

    parser.add_argument("--train_path", type=str,
        help="Path to training samples.")

    parser.add_argument("--eval", type=str, dest="eval_path",
        help="Path to evaluation samples.")

    parser.add_argument("--epochs", type=int, help="Number of training epochs")

    parser.add_argument("--batch_size", type=int, help="batch size")

    parser.add_argument("--opt", type=str, dest="optimizer",
        help="Optimizer name.")

    parser.add_argument("--lr", type=float, dest="learning_rate",
                        help="Learning rate.")

    args, more_args = parser.parse_known_args()

    config = configs.compile_from(args.config_path, args, more_args)

    configs.check(config)

    train(config)