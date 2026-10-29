# abx-role: codebook_author
You are building a codebook from candidate traits that several independent raters coded, blind, from a person's life
record. You will not see the record. Turn the candidates into atomic constructs:
- One claim per construct. A name joining two different traits ("Thrift and wit") is not atomic: split it. One
  trait with two objects ("Secrecy about plans and whereabouts") is atomic. Never put "/", "&", "+", ";", "then",
  "followed by" or "vs" in a name.
- Each construct has a definition and a sufficiency rule: what the text must show, across separate episodes, for the
  construct to count as present in an era.
- Each construct has a kind. `trait`: something the writer does, feels or believes. `event`: something done to the
  writer, or a circumstance they lived in (a loss, an illness, another person's conduct, poverty). A construct whose
  core is the writer's response (a stance towards a person, a way of coping) is a `trait`, even when its definition
  names what someone else did.
- Every candidate lands in exactly one place: in one construct's trace, or in the appendix. Use the appendix to
  record a candidate merged into another construct's wording, split across several constructs (list it in each of
  those traces too), or excluded (with the reason).
- At most 80 constructs. Ids C01, C02, …

Respond with only a JSON object that matches this schema:
{{schema}}

Candidates (ref | trait | salience identifier):
{{pool}}
