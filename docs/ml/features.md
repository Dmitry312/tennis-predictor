# Feature definitions

Rating features are experimental. At the start of each UTC day the rating
snapshot exposes own and opponent ratings, their difference, Glicko RD,
Glicko-2 volatility, prior match counts and prior inactivity. Surface Elo
uses the weighted effective rating for the target surface, with Global Elo
fallback when surface is unknown. A newcomer has no prior rated result in
that gender pool. Residual `actual − pre_match_probability` becomes known
after the result and may only describe prior matches. Its target-match value
must never enter the feature vector.

Use only states derived from dates strictly earlier than the prediction
date. Other longitudinal features remain on demand from Match Data Store.
Definitions and update equations are in [ratings.md](ratings.md).
