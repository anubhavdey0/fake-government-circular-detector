## Evaluation on real UGC documents

150 official PDFs sampled at random (seed 7) from 4,547 indexed. No OCR. Each file was turned into the variants below and checked against the full index.

| Variant | Cases | Expected verdict | Correct | Text-layer PDFs | Scanned PDFs |
|---|---:|---|---:|---:|---:|
| exact copy | 150 | VERIFIED_EXACT | 150/150 (100%) | 86/86 (100%) | 64/64 (100%) |
| page 1 saved as new PDF | 150 | VERIFIED_CONTENT or VERIFIED_EXACT | 149/150 (99%) | 86/86 (100%) | 63/64 (98%) |
| WhatsApp-style JPEG screenshot | 149 | VERIFIED_CONTENT | 148/149 (99%) | 85/85 (100%) | 63/64 (98%) |
| screenshot with a date painted in | 143 | ALTERED | 129/143 (90%) | 72/81 (89%) | 57/62 (92%) |
| PDF edited: white-out + typed text | 143 | ALTERED | 140/143 (98%) | 81/81 (100%) | 59/62 (95%) |
| letterhead kept, body fabricated | 149 | NO_MATCH | 149/149 (100%) | 85/85 (100%) | 64/64 (100%) |

Of the edited copies flagged as altered, **266/269** had a marked region overlapping the edit.

**Genuine documents missing from the index.** Each sampled file was also checked with itself removed from the index, which is what happens when a real notice is newer than the last crawl:

- NO_MATCH: 135 (90%)
- ALTERED: 14 (9%)
- VERIFIED_CONTENT: 1 (1%)

`VERIFIED_CONTENT` here means the issuer published the same document again under another entry (common for re-posted notices). `ALTERED` means a close relative exists, usually the same letter issued for another year, which is why the report tells you to refresh the index before concluding a recent notice was edited.

Median time per check: 0.99 s (90th percentile 1.62 s).
