# GLM-OCR reasoning regression corpus

`glm_ocr_reasoning.json` contains reasoning text from all 80 Worker turns in the supplied
`book-ocr-01.log`. The JSON records the original source filename and original log SHA-256. It is a self-contained fixture;
tests do not require the original full log or any provider connection.

Extraction: match each `━━ STEP N/80 ━━` followed by the timestamped `WORKER  reasoning` header;
take text up to the first `  └─ prompt ` usage line, and strip trailing layout whitespace.
Actions, shell output, usage telemetry and step headings are excluded. The detector receives only
the text values; the step labels are used only by the regression assertions.

The original STEP 37 ends with 7090 reported reasoning tokens and contains 6111 `\w+` words.
With production defaults, confirmation is at completed word 1160 (window starts at 920), matching
an older window starting at 400, Jaccard 0.9149797570850202, distance 520 words. No other captured
turn confirms. This corpus-level observation is not a general false-positive guarantee.

Publication redactions: the local username in STEP 9 is replaced by `developer`;
local network addresses in STEP 7 and STEP 9 are replaced by documentation-only
addresses `192.0.2.0` and `192.0.2.1`. The source SHA-256 identifies the original log,
not a checksum of this anonymized fixture. All 80 turns are retained; STEP 37 and
its expected detector measurements are unchanged.
