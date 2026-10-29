# abx-role: calibration_judge
You are labelling sentences from a person's autobiography for a research protocol. Each sentence describes the
writer's life either as it was then or as the writer understands it now. Your labels are compared with another
labeller's, so apply the rubric below exactly and label every sentence on its own terms.

Rubric:
- keep: the sentence reports something that happened, or what the writer felt, thought, wanted or decided at the time.
  It counts even when told from years later ("I was terrified of him", "That spring I resolved to leave Boston").
- remove: the sentence is the writer's view from the time of writing: a conclusion about their own character, a
  pattern they now see in their life, a lesson, or a judgement of their past self ("I now see this was the start of my
  need to prove myself", "It taught me never to trust a promise").
- mixed: one sentence does both.

A line starting `--` before a paragraph gives the nearest heading and the dates above it in the record, to help you
tell when a sentence is written about. It is not a sentence: give it no label.

Label every id below exactly once. Never quote, rewrite or summarise the text; return ids and labels only.
Respond with only a JSON object that matches this schema:
{{schema}}

Sentences:
{{excerpt}}
