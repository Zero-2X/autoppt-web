

---

## Service addendum (autoppt-web, not part of the source repository)

This section is appended by the AutoPPT web service when it builds a Worker template. The source
repository is read-only for the service and does not contain it.

### Source type `user_supplied_master` (decision D3)

In a vectorization job the frozen masters in `assets/slides/` are the user's own pages (a PPTX
rendered by PowerPoint, PDF pages, or images), recorded in `assets/slides/user-supplied-masters.json`
with the upload hash, page number and `provenance: user_supplied_master`. They are not ImageGen output.

- The ImageGen-first gate does not apply to them and must not be run, imitated or satisfied with a
  substitute report. The Worker runs its own gate instead (`verify_user_supplied_masters`): every master
  must still match its recorded hash and its upload, and no unlisted page may appear. It runs before the
  rebuild and before packaging; a failure blocks the job.
- Never redraw, regenerate or "improve" these pages with ImageGen or code. Reconstruct them as they are.
- Tools that require an ImageGen-first report (for example the measured composer's
  `--imagegen-first-report`) cannot be used for these jobs. Build the editable deck directly and
  measure it with the same Office render comparison.
- The exact visible text is `workspace/vectorize/reviewed-text.json`, the text the user checked at CP-V.
  OCR may suggest positions, never wording.
- The release tier is `user-supplied-master-native-approved`, approved by the user at CP3. Never call
  the result an ImageGen-first, Gold or `native-priority-approved` deck.
