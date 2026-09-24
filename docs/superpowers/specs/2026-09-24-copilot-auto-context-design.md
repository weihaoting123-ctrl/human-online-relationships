# COPILOT-02: local current-conversation title recognition

## Decision and authority

The maintainer requests automatic current-conversation recognition and has delegated
routine design choices. Implement a local title observer and automatic archive
candidate selection, not automatic cloud analysis or stable account authentication.
The existing Windows shell and scoped preview/confirmation workflow remain.

## Evidence and alternatives

UI Automation is preferable when a version exposes a supported title element, but
the inspected 4.1.13 UI exposes only outer rendering panes. Do not inject code,
write process memory, enable hidden accessibility flags, restart WeChat, or guess
identity from database modification times. This iteration uses the already
installed Windows OCR engine over a bounded conversation-header region. No OCR
dependency download, screenshots on disk, full-window capture, message reading,
clipboard reading or cloud OCR. Unsupported layouts fail unavailable. OCR labels
are untrusted data, not instructions or proof of peer/account identity.

## Architecture

1. A separate Windows PowerShell 5.1 worker uses built-in WinRT OCR and an isolated
   in-memory header bitmap. It identifies a unique supported WeChat window with the
   existing process/geometry restrictions, validates the 4.1.13.x version profile,
   and samples at a low rate only while WeChat or the assistant is foreground.
   It never brings a window to front. Main-process supervision bounds a stuck worker.
   The first use requires explicit calibration: mark the header's top-left and
   bottom-right with Ctrl+Alt+F8 within 60 seconds. No guessed region is captured.
   Only bounded numeric DIP coordinates and a fixed profile ID persist locally;
   title text and bitmap never persist. Moving the window reuses relative bounds;
   changed layout, font sizing or title truncation may require recalibration.
2. A small desktop observer controller validates bounded JSON frames, stabilizes
   successive identical labels, fences sequences and clears immediately on changes,
   ambiguity, pause, expiry, minimized window, unsupported layout or worker failure.
   A read-only preload stream delivers observation metadata only to the trusted page.
3. Backend resolver uses the existing metadata index without opening message files.
   Exact OCR-label to original archive-title matches only; no fuzzy matching, recency ranking or
   management-alias identity inference. Hidden/missing entries still cause ambiguity.
   Unique available matches are `suggested`, with `account_verified: false`.
4. An in-memory server binding token freezes the observation generation, candidate
   and metadata fingerprint. New observations revoke old tokens; out-of-order frames
   cannot restore them. Preview and the atomic send claim recheck the current token.
   Claim and revocation serialize under one lock; the network call is outside it.
5. Native UI defaults to automatic title recognition, offers manual mode, displays
   recognized title/candidate/uncertainty locally, and never auto-previews or sends.
   Switching conversation clears drafts, previews and results immediately. Cloud
   confirmation additionally requires an unchecked explicit candidate identity check.
   Browser mode remains manual and cannot claim native recognition.

## Privacy and limitations

Title-only matching may identify the wrong person even if the archive has one name:
an unarchived contact can have the same name. It is a candidate, not a verified ID.
OCR may mistake glyphs or merge meaningful CJK spaces: the Windows OCR output is
NFC-normalized and its inserted inter-CJK spaces are removed. This does not prove
the original displayed title is an exact match. The unchecked identity confirmation
therefore remains mandatory for each automatic candidate preview.
No group-member/unread suffix is silently removed. Empty/ellipsized/multiline/long
labels and uncertain OCR produce no candidate. Raw titles remain in volatile local
memory and are never written to logs, public fixtures, version-control or cloud
prompts. Source archives, sync, credentials and backups are not modified.

## Verification and delivery

Use synthetic title images/windows and archive metadata for deterministic tests:
normalization, duplicate/hidden collision, OCR failure/timeout, same-window A-B-A,
sequence reversal, paused/stale native observations, late HTTP/model results,
explicit candidate confirmation, and concurrent binding revocation/send claims.
Real-device checks return only status/counts/lengths, never names or screenshots.
Full Python/browser and Node regression, independent review and publication scans
remain required. Release as 1.0.0-rc.2 only after recorded results; do not claim
stable identity verification, full real-time message capture or a formal 1.0 release.

## References

- [Microsoft UI Automation scope](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-obtainingelements)
- [Microsoft UI Automation threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)
- [Built-in OCR engine](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.trycreatefromlanguage)
