# Changelog

## 0.3.0

Renamed from "Local OCR" to **Text Reader** (domain `text_reader`), and it can now read with an AI assistant.

- **Upgrading from Local OCR 0.2: remove the old "Local OCR" integration and add Text Reader.** The domain changed (`local_ocr` to `text_reader`), so there is no automatic migration. Change `local_ocr.read_image` to `text_reader.read_image` in your automations. Update the Local OCR App to 0.3.0 as well.
- Three ways to read, chosen at setup and in the options: on this device (the Local OCR App), with an AI Task entity you already have, or both (the assistant is asked only when the local read is weak; `auto_threshold`, default 0.8).
- New `backend` field on `text_reader.read_image`. The result has `routed` and `confidence`; assistant results have no boxes and `confidence: null`.
- Setup can install the Local OCR App for you on Supervisor installs, tests a sample with the chosen reader before saving, and steers 32-bit or low-memory Supervisor systems to the AI choice.
- AI use needs an explicit privacy confirmation in setup and options.
- New repairs: `ai_entity_missing`, `ai_entity_unavailable`, `local_unsupported_here`.
- Diagnostics add the backend, AI entity id, auto threshold, prompt version and `routed` per request. Still no text.
