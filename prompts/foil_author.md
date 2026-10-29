# abx-role: foil_author
You are writing test items for a blind trait-coding study of one era of a person's life record. You know nothing about
this person except the text below. Lines are sentence ids; `[…]` marks removed material. Raters will later score many
trait items against this era without knowing which are tests; your items let the study measure how often they err.

Write one to three items, ids F1 to F3. Each reads like a trait a rater might code: an atomic name (one claim, no
"/", "&", "+", ";", "then", "followed by" or "vs"), a one-or-two-sentence definition, and a rule for when the era
shows it. Give each one type, with the evidence that type needs, citing only sentences in this era:
- clear: a trait this era plainly lacks. Cite at least one counter-sentence showing the era would have shown it and
  does not, or shows the opposite. No support.
- near_miss: a tempting over-generalisation. Cite at least one support occurrence that makes it plausible (sentence
  ids and the event) and at least one counter-sentence that defeats it.
- anchored: a trait this era plainly shows. Cite at least two support occurrences that are separate episodes in
  different, non-adjacent paragraphs, each with its sentence ids and the event.
Per item, cite at most 6 support occurrences and at most 6 counter-sentences (the clearest), each occurrence with at
most 12 sentence ids. Write only items whose answer for this era is clear from the text; prefer fewer items to doubtful
ones.

Respond with only a JSON object that matches this schema:
{{schema}}

Era: {{era_label}}
{{era}}
