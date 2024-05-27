from typing import List
import torch
from PIL import Image

from surya.postprocessing.math.latex import fix_math, contains_math
from surya.postprocessing.text import truncate_repetitions
from surya.settings import settings
from tqdm import tqdm
import numpy as np
import torch.nn.functional as F


def get_batch_size():
    batch_size = settings.RECOGNITION_BATCH_SIZE
    if batch_size is None:
        batch_size = 32
        if settings.TORCH_DEVICE_MODEL == "mps":
            batch_size = 64 # 12GB RAM max
        if settings.TORCH_DEVICE_MODEL == "cuda":
            batch_size = 256
    return batch_size


def batch_recognition(images: List, languages: List[List[str]], model, processor, batch_size=None):
    assert all([isinstance(image, Image.Image) for image in images])
    assert len(images) == len(languages)
    assert [len(l) <= settings.RECOGNITION_MAX_LANGS for l in languages], f"OCR only supports up to {settings.RECOGNITION_MAX_LANGS} languages per image"

    images = [image.convert("RGB") for image in images]
    if batch_size is None:
        batch_size = get_batch_size()

    output_text = []
    confidences = []

    if settings.RECOGNITION_STATIC_CACHE:
        min_val = torch.finfo(model.dtype).min
        kv_mask = torch.full((batch_size, 1, 1, settings.RECOGNITION_MAX_TOKENS + 1), min_val, dtype=model.dtype, device=model.device)
        # The +1 accounts for start token
        orig_attention_mask = torch.full((batch_size, 1, settings.RECOGNITION_MAX_LANGS + 1, settings.RECOGNITION_MAX_LANGS + 1), min_val, dtype=model.dtype, device=model.device)

    processed_batches = processor(text=[""] * len(images), images=images, lang=languages)

    for i in tqdm(range(0, len(images), batch_size), desc="Recognizing Text"):
        batch_langs = languages[i:i+batch_size]
        has_math = ["_math" in lang for lang in batch_langs]

        batch_pixel_values = processed_batches["pixel_values"][i:i+batch_size]
        batch_langs = processed_batches["langs"][i:i+batch_size]
        batch_decoder_input = [[model.config.decoder_start_token_id] + lang for lang in batch_langs]
        current_batch_size = len(batch_pixel_values)

        batch_langs = torch.tensor(np.vstack(batch_langs), dtype=torch.long, device=model.device)
        batch_pixel_values = torch.tensor(np.vstack(batch_pixel_values), dtype=model.dtype, device=model.device)
        batch_decoder_input = torch.tensor(np.vstack(batch_decoder_input), dtype=torch.long, device=model.device)

        token_count = 0
        inference_token_count = batch_decoder_input.shape[-1]
        batch_predictions = [[] for _ in range(current_batch_size)]

        # Decoder kv cache
        # 7 (layers) x 2 (kv) x bs x 4 (heads) x max tokens x 64 (head dim)
        dec_config = model.config.decoder
        layer_count = dec_config.decoder_layers
        kv_heads = dec_config.kv_heads
        head_dim = int(dec_config.d_model / dec_config.decoder_attention_heads)
        decoder_input_pad = torch.zeros((batch_size - current_batch_size, 1), dtype=torch.long, device=model.device)

        if settings.RECOGNITION_STATIC_CACHE:
            decoder_cache = [torch.zeros((2, batch_size, kv_heads, settings.RECOGNITION_MAX_TOKENS, head_dim), dtype=model.dtype, device=model.device) for _ in range(layer_count)]

            # Reset kv mask
            kv_mask.fill_(min_val)
            kv_mask[:, :, :, -1] = 0
            kv_mask[:, :, :, :inference_token_count] = 0

            # Reset attention mask
            orig_attention_mask.fill_(min_val)
            attention_mask = orig_attention_mask
            attention_mask[:, :, -inference_token_count:, -inference_token_count:] = 0

            decoder_input = torch.zeros((batch_size, settings.RECOGNITION_MAX_LANGS + 1), dtype=torch.long, device=model.device)
            decoder_input[:current_batch_size, -inference_token_count:] = batch_decoder_input
            batch_decoder_input = decoder_input

            batch_langs = torch.cat([batch_langs, torch.zeros((batch_size - current_batch_size, batch_langs.shape[-1]), dtype=torch.long, device=model.device)], dim=0)
            batch_pixel_values = torch.cat([batch_pixel_values, torch.zeros((batch_size - current_batch_size,) + batch_pixel_values.shape[1:], dtype=model.dtype, device=model.device)], dim=0)
        else:
            kv_mask = torch.zeros((current_batch_size, 1, 1, 1), dtype=model.dtype, device=model.device)
            decoder_cache = [None] * layer_count
            attention_mask = torch.zeros((current_batch_size, 1, inference_token_count, inference_token_count), dtype=model.dtype, device=model.device)

        encoder_outputs = None
        sequence_scores = None
        encoder_cache = [None] * layer_count
        all_done = torch.zeros(current_batch_size, dtype=torch.bool, device=model.device)

        with torch.no_grad():
            # Run post-prefill tokens
            while token_count < settings.RECOGNITION_MAX_TOKENS:
                is_prefill = token_count == 0
                return_dict = model(
                    decoder_input_ids=batch_decoder_input,
                    decoder_attention_mask=attention_mask,
                    decoder_self_kv_cache=None if is_prefill else decoder_cache,
                    decoder_cross_kv_cache=None if is_prefill else encoder_cache,
                    decoder_past_token_count=token_count,
                    decoder_langs=batch_langs,
                    pixel_values=batch_pixel_values,
                    encoder_outputs=encoder_outputs,
                    return_dict=True,
                )

                logits = return_dict["logits"][:current_batch_size]
                preds = torch.argmax(logits[:, -1], dim=-1)
                scores = torch.max(F.softmax(logits, dim=-1), dim=-1).values
                done = (preds == processor.tokenizer.eos_id) | (preds == processor.tokenizer.pad_id)
                done = done
                all_done = all_done | done

                scores[all_done == 1] = 0

                if is_prefill:
                    sequence_scores = scores
                    encoder_outputs = (return_dict["encoder_last_hidden_state"],)
                else:
                    sequence_scores = torch.cat([sequence_scores, scores], dim=1)

                if all_done.all():
                    break

                past_key_values = return_dict["past_key_values"]
                token_range = torch.arange(token_count, token_count + inference_token_count, device=model.device)

                for layer_idx, layer in enumerate(past_key_values):
                    if is_prefill:
                        encoder_cache[layer_idx] = layer[1]

                    if settings.RECOGNITION_STATIC_CACHE:
                        decoder_cache[layer_idx][:, :, :, token_range, :] = layer[0][:, :, :, -inference_token_count:, :]
                    else:
                        if is_prefill:
                            decoder_cache[layer_idx] = layer[0]
                        else:
                            decoder_cache[layer_idx] = torch.cat([decoder_cache[layer_idx], layer[0]], dim=3)

                batch_decoder_input = preds.unsqueeze(1)
                if settings.RECOGNITION_STATIC_CACHE:
                    kv_mask[:, :, :, token_count:(token_count + inference_token_count)] = 0
                    batch_decoder_input = torch.cat([batch_decoder_input, decoder_input_pad], dim=0)
                else:
                    kv_mask = torch.cat([kv_mask, torch.zeros((current_batch_size, 1, 1, inference_token_count), dtype=model.dtype, device=model.device)], dim=-1)

                attention_mask = kv_mask

                for j, (pred, status) in enumerate(zip(preds, all_done)):
                    if not status:
                        batch_predictions[j].append(int(pred))

                token_count += inference_token_count
                inference_token_count = batch_decoder_input.shape[-1]

        sequence_scores = torch.sum(sequence_scores, dim=-1) / torch.sum(sequence_scores != 0, dim=-1)
        detected_text = processor.tokenizer.batch_decode(batch_predictions)
        detected_text = [truncate_repetitions(dt) for dt in detected_text]

        # Postprocess to fix LaTeX output (add $$ signs, etc)
        detected_text = [fix_math(text) if math and contains_math(text) else text for text, math in zip(detected_text, has_math)]
        output_text.extend(detected_text)
        confidences.extend(sequence_scores.tolist())

    return output_text, confidences






