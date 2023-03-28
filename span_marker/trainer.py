from typing import Callable, Dict, List, Optional, Tuple, Union

import evaluate
import numpy as np
import torch
from datasets import Dataset
from torch import nn
from torch.nn import functional as F
from transformers import (
    DataCollator,
    DataCollatorForTokenClassification,
    EvalPrediction,
    PreTrainedModel,
    PreTrainedTokenizerBase,
    TrainerCallback,
    TrainingArguments,
)
from transformers import (
    Trainer as TransformersTrainer,
)

from span_marker.configuration import SpanMarkerConfig
from span_marker.data.data_collator import SpanMarkerDataCollator
from span_marker.data.label_normalizer import AutoLabelNormalizer
from span_marker.modeling import SpanMarkerModel
from span_marker.tokenizer import SpanMarkerTokenizer


def compute_f1_via_seqeval(tokenizer: SpanMarkerTokenizer, eval_prediction: EvalPrediction):
    inputs = eval_prediction.inputs
    gold_labels = eval_prediction.label_ids
    logits = eval_prediction.predictions[0]
    num_words = eval_prediction.predictions[-1]
    probs = F.softmax(torch.tensor(logits), dim=-1)
    scores, pred_labels = probs.max(-1)
    assert inputs.shape[0] == num_words.shape[0] == gold_labels.shape[0] == pred_labels.shape[0]

    # Collect all samples in one dict. We do this because some samples are spread between multiple inputs
    sample_dict = {}
    for sample_idx in range(inputs.shape[0]):
        tokens = inputs[sample_idx]
        eos_idx = tokens.tolist().index(tokenizer.tokenizer.eos_token_id)
        text = tokenizer.decode(tokens[: eos_idx + 1], skip_special_tokens=True)
        # token_hash = hash(tokens.data.tobytes())
        token_hash = hash(text)
        if token_hash not in sample_dict:
            # TODO: Avoid having to have a config on the tokenizer
            spans = list(tokenizer.get_all_valid_spans(num_words[sample_idx], tokenizer.config.max_entity_length))
            sample_dict[token_hash] = {
                "text": text,
                "spans": spans,
                "gold_labels": gold_labels[sample_idx].tolist(),
                "pred_labels": pred_labels[sample_idx].tolist(),
                "scores": scores[sample_idx].tolist(),
                "num_words": num_words[sample_idx],
            }
        else:
            sample_dict[token_hash]["gold_labels"] += gold_labels[sample_idx].tolist()
            sample_dict[token_hash]["pred_labels"] += pred_labels[sample_idx].tolist()
            sample_dict[token_hash]["scores"] += scores[sample_idx].tolist()
    # TODO: We assume that samples are not shuffled for the evaluation/prediction
    # With other words, don't use this on the train dataset!

    outside_id = tokenizer.config.outside_id
    id2label = tokenizer.config.id2label
    # all_gold_labels = []
    # all_pred_labels = []
    seqeval = evaluate.load("seqeval")
    for sample in sample_dict.values():
        text = sample["text"]
        spans = sample["spans"]
        scores = sample["scores"]
        num_words = sample["num_words"]
        gold_labels = sample["gold_labels"]
        pred_labels = sample["pred_labels"]
        gold_labels_per_tokens = ["O"] * num_words
        # assert len(spans) <= len(gold_labels)
        for span, gold_label in zip(spans, gold_labels):
            assert gold_label >= 0
            if gold_label != outside_id:
                # print(span, gold_label)
                gold_labels_per_tokens[span[0]] = "B-" + id2label[gold_label]
                gold_labels_per_tokens[span[0] + 1 : span[1]] = ["I-" + id2label[gold_label]] * (span[1] - span[0] - 1)
        # TODO: Be less naive about overlapping:
        pred_labels_per_tokens = ["O"] * num_words
        for _, span, pred_label in sorted(zip(scores, spans, pred_labels), key=lambda tup: tup[0], reverse=True):
            assert pred_label >= 0
            # print([pred_labels_per_tokens[i] == "O" for i in range(span[0], span[1])])
            if pred_label != outside_id and all(pred_labels_per_tokens[i] == "O" for i in range(span[0], span[1])):
                # print(span, pred_label)
                pred_labels_per_tokens[span[0]] = "B-" + id2label[pred_label]
                pred_labels_per_tokens[span[0] + 1 : span[1]] = ["I-" + id2label[pred_label]] * (span[1] - span[0] - 1)
                # print(span, pred_label, score)

        seqeval.add(prediction=pred_labels_per_tokens, reference=gold_labels_per_tokens)
        # pprint(seqeval.compute())
        # pprint(list(zip(gold_labels_per_tokens, pred_labels_per_tokens)))
        # breakpoint()

    result = seqeval.compute()
    # breakpoint()

    return {key: value for key, value in result.items() if isinstance(value, float)}

    """
    breakpoint()

    label_mask = (gold_labels != -100)# & (gold_labels != 0)
    total_labels = label_mask.sum()
    correct_labels = ((gold_labels == pred_labels) & label_mask).sum()
    baseline = ((gold_labels == 0) & label_mask).sum()
    print(f"A total of {correct_labels / total_labels:.2%} of all spans in the evaluation dataset are classified correctly.")
    print(f"Compared to {baseline / total_labels:.2%} if we classified everything as 'Outside'.")
    breakpoint()
    return {"accuracy": correct_labels / total_labels}
    """


class Trainer(TransformersTrainer):
    def __init__(
        self,
        model: SpanMarkerModel = None,
        args: TrainingArguments = None,
        data_collator: Optional[DataCollator] = None,
        train_dataset: Optional[Dataset] = None,
        eval_dataset: Optional[Dataset] = None,
        tokenizer: SpanMarkerTokenizer = None,
        model_init: Callable[[], PreTrainedModel] = None,
        compute_metrics: Optional[Callable[[EvalPrediction], Dict]] = None,
        callbacks: Optional[List[TrainerCallback]] = None,
        optimizers: Tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR] = (None, None),
        preprocess_logits_for_metrics: Callable[[torch.Tensor, torch.Tensor], torch.Tensor] = None,
    ) -> None:
        # TODO: Disallow custom compute_metrics and data_collator?

        # Ensure that the model is resized to accompany the updated tokenizer (i.e. the added "<start>" and "<end>")
        model.resize_token_embeddings(len(tokenizer))

        # Convert dataset labels to a common format (list of label-start-end tuples)
        label_normalizer = AutoLabelNormalizer.from_config(model.config)
        if train_dataset:
            train_dataset = train_dataset.map(label_normalizer, input_columns="ner_tags", batched=True)
            # Tokenize and add start/end markers
            train_dataset = train_dataset.map(
                lambda batch: tokenizer(batch["tokens"], config=model.config, labels=batch["ner_tags"]),
                batched=True,
                remove_columns=train_dataset.column_names,
            )
        if eval_dataset:
            eval_dataset = eval_dataset.map(label_normalizer, input_columns="ner_tags", batched=True)
            # Tokenize and add start/end markers, return tokens for use in the metrics computations
            eval_dataset = eval_dataset.map(
                lambda batch: tokenizer(
                    batch["tokens"], config=model.config, labels=batch["ner_tags"], return_num_words=True
                ),
                batched=True,
                remove_columns=eval_dataset.column_names,
            )

        if data_collator is None:
            data_collator = SpanMarkerDataCollator(
                tokenizer=tokenizer, max_marker_length=model.config.max_marker_length
            )
        if args is None:
            args = TrainingArguments()
        args.include_inputs_for_metrics = True
        args.remove_unused_columns = False
        super().__init__(
            model=model,
            args=args,
            data_collator=data_collator,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            tokenizer=tokenizer,
            model_init=model_init,
            compute_metrics=lambda eval_prediction: compute_f1_via_seqeval(tokenizer, eval_prediction),
            callbacks=callbacks,
            optimizers=optimizers,
            preprocess_logits_for_metrics=preprocess_logits_for_metrics,
        )