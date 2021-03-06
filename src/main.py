"""
Provides options via the command line to perform project tasks.
* `--dataset`: dataset name (bentham, iam, rimes, saintgall, washington)
* `--arch`: network to be used (puigcerver, bluche, flor)
* `--transform`: transform dataset to the HDF5 file
* `--cv2`: visualize sample from transformed dataset
* `--train`: train model with the dataset argument
* `--test`: evaluate and predict model with the dataset argument
* `--epochs`: number of epochs
* `--batch_size`: number of batches
"""

import os
import importlib
import argparse
import h5py
import cv2
import time

from multiprocessing import Pool
from functools import partial
from data import preproc as pp, evaluation
from data.generator import DataGenerator
from network import architecture, callbacks
from network.model import HTRModel


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, required=True)
    parser.add_argument("--arch", type=str, default="flor")
    parser.add_argument("--transform", action="store_true", default=False)
    parser.add_argument("--cv2", action="store_true", default=False)
    parser.add_argument("--train", action="store_true", default=False)
    parser.add_argument("--test", action="store_true", default=False)
    parser.add_argument("--epochs", type=int, default=1000)
    parser.add_argument("--batch_size", type=int, default=16)
    args = parser.parse_args()

    raw_path = os.path.join("..", "raw", args.dataset)
    hdf5_src = os.path.join("..", "data", f"{args.dataset}.hdf5")
    output_path = os.path.join("..", "output", args.dataset, args.arch)

    input_size = (1024, 128, 1)
    max_text_length = 128
    charset_base = "".join([chr(i) for i in range(32, 127)])

    if args.transform:
        assert os.path.exists(raw_path)
        print(f"The {args.dataset} dataset will be transformed...")

        mod = importlib.import_module(f"transform.{args.dataset}")
        os.makedirs(os.path.dirname(hdf5_src), exist_ok=True)

        dtgen = mod.Dataset(partitions=["train", "valid", "test"])
        dataset = dtgen.get_partitions(source=raw_path)

        for i in dtgen.partitions:
            dataset[i]["gt"] = pp.standardize_texts(dataset[i]["gt"])
            dataset[i]["gt"] = pp.encode_ctc(dataset[i]["gt"], charset_base, max_text_length)

            pool = Pool()
            dataset[i]["dt"] = pool.map(partial(pp.preproc, img_size=input_size), dataset[i]["dt"])
            pool.close()
            pool.join()

            with h5py.File(hdf5_src, "a") as hf:
                hf.create_dataset(f"{i}/gt", data=dataset[i]["gt"], compression="gzip", compression_opts=9)
                hf.create_dataset(f"{i}/dt", data=dataset[i]["dt"], compression="gzip", compression_opts=9)
                dataset[i] = None
                print(f"[OK] {i} partition.")

        print(f"Transformation finished.")

    elif args.cv2:
        with h5py.File(hdf5_src, "r") as hf:
            dt = hf["train"]["dt"][:]
            gt = hf["train"]["gt"][:]

        for x in range(len(dt)):
            print(f"Image shape: {dt[x].shape}")
            print(f"Ground truth: {pp.decode_ctc([gt[x]], charset_base)[0]}")
            print(f"Ground truth sparsed:\n{gt[x]}\n")

            cv2.imshow("img", pp.adjust_to_see(dt[x]))
            cv2.waitKey(0)

    elif args.train or args.test:
        os.makedirs(output_path, exist_ok=True)

        dtgen = DataGenerator(hdf5_src=hdf5_src,
                              batch_size=args.batch_size,
                              max_text_length=max_text_length)

        network_func = getattr(architecture, args.arch)
        ioo = network_func(input_size=input_size, output_size=len(charset_base) + 1)

        model = HTRModel(inputs=ioo[0], outputs=ioo[1], charset=charset_base)
        model.compile(optimizer=ioo[2])

        checkpoint = "checkpoint_weights.hdf5"
        model.load_checkpoint(target=os.path.join(output_path, checkpoint))

        if args.train:
            model.summary(output_path, "summary.txt")
            cbs = callbacks.setup(logdir=output_path, hdf5=checkpoint)

            start_time = time.time()
            h = model.fit_generator(generator=dtgen.next_train_batch(),
                                    epochs=args.epochs,
                                    steps_per_epoch=dtgen.train_steps,
                                    validation_data=dtgen.next_valid_batch(),
                                    validation_steps=dtgen.valid_steps,
                                    callbacks=cbs,
                                    shuffle=True,
                                    verbose=1)
            total_time = time.time() - start_time

            loss = h.history['loss']
            val_loss = h.history['val_loss']

            min_val_loss = min(val_loss)
            min_val_loss_i = val_loss.index(min_val_loss)

            train_corpus = "\n".join([
                f"Total train images:       {dtgen.total_train}",
                f"Total validation images:  {dtgen.total_valid}",
                f"Batch:                    {args.batch_size}\n",
                f"Total time:               {total_time:.8f} sec",
                f"Average time per epoch:   {(total_time / len(loss)):.8f} sec\n",
                f"Total epochs:             {len(loss)}",
                f"Best epoch                {min_val_loss_i + 1}\n",
                f"Training loss:            {loss[min_val_loss_i]:.8f}",
                f"Validation loss:          {min_val_loss:.8f}"
            ])

            with open(os.path.join(output_path, "train.txt"), "w") as lg:
                lg.write(train_corpus)
                print(train_corpus)

        elif args.test:
            start_time = time.time()
            predicts = model.predict_generator(generator=dtgen.next_test_batch(),
                                               steps=dtgen.test_steps,
                                               use_multiprocessing=True,
                                               verbose=1)

            predicts = pp.decode_ctc(predicts, charset_base)
            total_time = time.time() - start_time

            ground_truth = pp.decode_ctc(dtgen.dataset["test"]["gt"], charset_base)
            pred_corpus = []

            for pd, gt in zip(predicts, ground_truth):
                pred_corpus.append(f"TE_L {gt}\nTE_P {pd}\n")

            with open(os.path.join(output_path, "predict.txt"), "w") as lg:
                lg.write("\n".join(pred_corpus))
                print("\n".join(pred_corpus))

            evaluate = evaluation.ocr_metrics(predicts=predicts,
                                              ground_truth=ground_truth,
                                              norm_accentuation=False,
                                              norm_punctuation=False)

            eval_corpus = "\n".join([
                f"Total test images:    {dtgen.total_test}",
                f"Total time:           {total_time:.8f} sec",
                f"Time per item:        {(total_time / dtgen.total_test):.8f} sec\n",
                f"Metrics:",
                f"Character Error Rate: {evaluate[0]:.8f}",
                f"Word Error Rate:      {evaluate[1]:.8f}"
            ])

            with open(os.path.join(output_path, "evaluate.txt"), "w") as lg:
                lg.write(eval_corpus)
                print(eval_corpus)
