from typing import Any, Dict
import logging

from sentence_transformers import SentenceTransformer
import torch
import torch.nn as nn
import transformers


MODEL_NAMES = [
    "bert",
    "contriever",
    "dpr",
    "gtr_base",
    "gtr_base__random_init",
    "gtr_large",
    "ance_tele",
    "dpr_st",
    "gtr_base_st",
    "paraphrase-distilroberta",
]
FREEZE_STRATEGIES = ["decoder", "encoder_and_decoder", "encoder", "none"]
EMBEDDING_TRANSFORM_STRATEGIES = ["repeat", "nearest_neighbors"]


logger = logging.getLogger(__name__)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def disable_dropout(model: nn.Module):
    dropout_modules = [m for m in model.modules() if isinstance(m, nn.Dropout)]
    for m in dropout_modules:
        m.p = 0.0
    print(
        f"Disabled {len(dropout_modules)} dropout modules from model type {type(model)}"
    )


def freeze_params(model: nn.Module):
    total_num_params = 0
    for name, params in model.named_parameters():
        params.requires_grad = False
        total_num_params += params.numel()
    print(f"Froze {total_num_params} params from model type {type(model)}")


def mean_pool(
    hidden_states: torch.Tensor, attention_mask: torch.Tensor
) -> torch.Tensor:
    B, S, D = hidden_states.shape
    unmasked_outputs = hidden_states * attention_mask[..., None]
    pooled_outputs = unmasked_outputs.sum(dim=1) / attention_mask.sum(dim=1)[:, None]
    assert pooled_outputs.shape == (B, D)
    return pooled_outputs


def max_pool(hidden_states: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    B, S, D = hidden_states.shape
    unmasked_outputs = hidden_states * attention_mask[..., None]
    pooled_outputs = unmasked_outputs.max(dim=1).values
    assert pooled_outputs.shape == (B, D)
    return pooled_outputs


def stack_pool(
    hidden_states: torch.Tensor, attention_mask: torch.Tensor
) -> torch.Tensor:
    B, S, D = hidden_states.shape
    unmasked_outputs = hidden_states * attention_mask[..., None]
    pooled_outputs = unmasked_outputs.reshape((B, S * D))  # stack along seq length
    assert pooled_outputs.shape == (B, S * D)
    return pooled_outputs

def load_embedder_and_tokenizer(name: str):
    # TODO make abstract/argparse for it etc.
    model_kwargs = {
        "low_cpu_mem_usage": True,
        "output_hidden_states": True,
    }

    if name == "dpr":
        # model = SentenceTransformer("sentence-transformers/facebook-dpr-question_encoder-multiset-base")
        model = transformers.DPRContextEncoder.from_pretrained(
            "facebook/dpr-ctx_encoder-single-nq-base"
        )
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            "facebook/dpr-ctx_encoder-single-nq-base"
        )
    elif name == "dpr_st":
        # TODO figure out why model w/ sentence transformers gives different results.
        model = SentenceTransformer(
            "sentence-transformers/facebook-dpr-question_encoder-multiset-base"
        )
        tokenizer = model.tokenizer
    elif name == "contriever":
        model = transformers.AutoModel.from_pretrained(
            "facebook/contriever", **model_kwargs
        )
        tokenizer = transformers.AutoTokenizer.from_pretrained("facebook/contriever")
    elif name == "bert":
        model = transformers.AutoModel.from_pretrained(
            "bert-base-uncased", **model_kwargs
        )
        tokenizer = transformers.AutoTokenizer.from_pretrained("bert-base-uncased")
    elif name == "gtr_base":
        model = transformers.AutoModel.from_pretrained(
            "sentence-transformers/gtr-t5-base", **model_kwargs
        ).encoder
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            "sentence-transformers/gtr-t5-base"
        )
    elif name == "gtr_base__random_init":
        config = transformers.AutoConfig.from_pretrained(
            "sentence-transformers/gtr-t5-base"
        )
        model = transformers.AutoModel.from_config(config).encoder
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            "sentence-transformers/gtr-t5-base"
        )
    elif name == "gtr_base_st":
        # TODO figure out why model w/ sentence transformers gives different results.
        model = SentenceTransformer("sentence-transformers/gtr-t5-base")
        tokenizer = model.tokenizer
    elif name == "gtr_large":
        model = SentenceTransformer("sentence-transformers/gtr-t5-large")
        tokenizer = model.tokenizer
    elif name == "ance_tele":
        model = transformers.AutoModel.from_pretrained(
            "OpenMatch/ance-tele_nq_psg-encoder", **model_kwargs
        )
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            "OpenMatch/ance-tele_nq_psg-encoder"
        )
    elif name == "paraphrase-distilroberta":
        model = transformers.AutoModel.from_pretrained(
            "sentence-transformers/paraphrase-distilroberta-base-v1", **model_kwargs
        )
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            "sentence-transformers/paraphrase-distilroberta-base-v1"
        )
    else:
        raise ValueError(f"unknown embedder {name}")

    # model = torch.compile(model)
    return model, tokenizer


class PrefixReranker(nn.Module):
    """Given an embedding prefix, predicts the final embedding of the completion
    of that prefix.
    """

    prefix_embedder: nn.Module  # embeds a prefix
    embedding_projection: nn.Module  # projects sentence embedding to same
    # space as a prefix embedding

    def __init__(
        self,
        prefix_embedder: nn.Module,
    ):
        super().__init__()
        self.prefix_embedder = prefix_embedder
        self.embedding_projection = nn.Sequential(
            nn.Linear(768, 2048),
            nn.GELU(),
            nn.Linear(2048, 768),
        )
        self.score = nn.Linear(768, 1)

    def score_prefix_and_embedding(
        self,
        prefix_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        embeddings: torch.Tensor,
    ) -> torch.Tensor:
        batch_size, prefix_length = prefix_ids.shape
        embeddings = self.embedding_projection(embeddings)
        inputs_embeds = self.prefix_embedder.encoder.embed_tokens(prefix_ids)
        all_embeddings = torch.cat((embeddings[:, None], inputs_embeds), dim=1)
        ones = torch.ones((batch_size, 1), device=attention_mask.device)
        attention_mask = torch.cat((ones, attention_mask), dim=1)
        model_output = self.prefix_embedder(
            inputs_embeds=all_embeddings, attention_mask=attention_mask
        )
        hidden_state = model_output.last_hidden_state
        output_embeddings = hidden_state[:, 0, :]
        # output_embeddings = mean_pool(hidden_state, attention_mask)
        scores = self.score(output_embeddings)
        scores = scores.flatten()  # (batch_size, 1) -> (batch_size,)
        return scores



def load_encoder_decoder(
    model_name: str, lora: bool = False
) -> transformers.AutoModelForSeq2SeqLM:
    model_kwargs: Dict[str, Any] = {
        "low_cpu_mem_usage": True,
    }
    if lora:
        model_kwargs.update(
            {
                "load_in_8bit": True,
                "device_map": "auto",
            }
        )
    return transformers.AutoModelForSeq2SeqLM.from_pretrained(
        model_name, **model_kwargs
    )
