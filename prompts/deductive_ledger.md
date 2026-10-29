# abx-role: deductive_ledger
You are an observer building an evidence ledger for one era of a person's life record, against the 30 facets of the
Five-Factor Model. You know nothing about this person except the text below. Lines are sentence ids; `[…]` marks
removed material.

For each facet, list the evidence this era gives, if any, at most 8 entries (the clearest): the sentence ids (at most
12 per entry), whether it points to the high or the low pole, and a short note. Record only what the text shows; leave
a facet's evidence empty when the era says nothing.
List all 30 facets.

Facets:
{{facets}}

Respond with only a JSON object that matches this schema:
{{schema}}

Era: {{era_label}}
{{era}}
