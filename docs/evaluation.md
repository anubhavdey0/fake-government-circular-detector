## Evaluation

100 official UGC PDFs sampled at random (seed 7) from the 4,922 indexed PDFs UGC currently lists, 3 debunked fakes from PIB Fact Check, and 40 genuine NTA circulars. No OCR. Every case is checked against the full index.

| Headline | Result |
|---|---|
| False verified: not an official UGC document, but called Verified | **0 of 448** (0.0%) |
| Altered notices caught (Altered or Known fake) | 253 of 285 (89%); 25 more matched with details unconfirmed, none verified |
| Edits caught with a marked region on the edit | 186 of 186 (mean IoU 0.59) |
| Flagging "not verified" against "not official": precision / recall / F1 | 0.794 / 1.000 / 0.885 |
| Genuine copies not verified (false-positive rate) | 116 of 497 (23.3%), of which 99 matched but with details unconfirmed |
| Original found among the top 1 / top 3 candidates (published and forwarded copies) | 97.0% / 97.0% |
| Index coverage of files UGC lists | 99.6% |
| Median time per check | 3.92 s (90th percentile 8.43 s), measured on an otherwise idle laptop; the latest run shared the machine with other work and gave the same results more slowly |

| Group | Variant | Expected | Correct | Text-layer PDFs | Scanned PDFs |
|---|---|---|---:|---:|---:|
| 1. Published copy | exact copy | VERIFIED_EXACT | 100/100 (100%) | 76/76 (100%) | 24/24 (100%) |
| 1. Published copy | page 1 saved as a new PDF | VERIFIED_CONTENT or VERIFIED_EXACT | 100/100 (100%) | 76/76 (100%) | 24/24 (100%) |
| 2. Forwarded | WhatsApp-quality JPEG | UNCONFIRMED_MATCH or VERIFIED_CONTENT or VERIFIED_EXACT | 98/99 (99%) | 74/75 (99%) | 24/24 (100%) |
| 2. Forwarded | phone screenshot with black bars | UNCONFIRMED_MATCH or VERIFIED_CONTENT or VERIFIED_EXACT | 93/99 (94%) | 70/75 (93%) | 23/24 (96%) |
| 2. Forwarded | angled phone photo | UNCONFIRMED_MATCH or VERIFIED_CONTENT or VERIFIED_EXACT | 89/99 (90%) | 67/75 (89%) | 22/24 (92%) |
| 3. Altered | screenshot with a date painted in | ALTERED | 91/95 (96%) | 67/71 (94%) | 24/24 (100%) |
| 3. Altered | PDF edited: white-out + typed text | ALTERED | 95/95 (100%) | 71/71 (100%) | 24/24 (100%) |
| 3. Altered | phone photo of an edited printout | ALTERED | 67/95 (71%) | 54/71 (76%) | 13/24 (54%) |
| 4. Debunked fakes | fake, forwarded copy | KNOWN_FAKE | 12/12 (100%) | n/a | n/a |
| 4. Debunked fakes | fake, store switched off | ALTERED or NO_MATCH | 12/12 (100%) | n/a | n/a |
| 5. Other issuer | NTA circular | NO_MATCH | 40/40 (100%) | n/a | n/a |
| Stress | UGC letterhead, fabricated body | NO_MATCH | 99/99 (100%) | 75/75 (100%) | 24/24 (100%) |
| Stress | genuine UGC notice missing from the index | ALTERED or NO_MATCH or UNCONFIRMED_MATCH or VERIFIED_CONTENT | 100/100 (100%) | 76/76 (100%) | 24/24 (100%) |

**Genuine notices missing from the index** (what a stale index sees): NO_MATCH 83, ALTERED 17. Altered here means a close relative exists, usually the same letter issued for another year; the report shows the differences and suggests refreshing the index.

