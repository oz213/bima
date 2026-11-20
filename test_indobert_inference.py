import torch
from transformers import (
    AutoTokenizer,
    AutoModelForSequenceClassification,
)

path = "/home/fauzi/crawler-rebuild/models/indobert-judol"

torch.set_num_threads(2)

tokenizer = AutoTokenizer.from_pretrained(
    path,
    local_files_only=True,
)

model, info = AutoModelForSequenceClassification.from_pretrained(
    path,
    local_files_only=True,
    output_loading_info=True,
)

print("Missing keys:", info["missing_keys"])
print("Unexpected keys:", info["unexpected_keys"])
print("Errors:", info["error_msgs"])

assert not info["missing_keys"]
assert not info["unexpected_keys"]
assert not info["error_msgs"]

model.eval()

samples = {
    "Judi": "Daftar sekarang di situs slot online, bonus deposit dan jackpot.",
    "Non-judi": "Sekolah menyelenggarakan kegiatan literasi digital untuk siswa.",
}

for label, text in samples.items():
    encoded = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=256,
    )

    with torch.inference_mode():
        logits = model(**encoded).logits
        probs = torch.softmax(logits, dim=-1)[0]

    print(label, {
        "non_gambling": round(probs[0].item(), 6),
        "gambling": round(probs[1].item(), 6),
    })
