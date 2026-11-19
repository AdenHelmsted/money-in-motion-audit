-- #GOAL Index Form D name candidates while preserving the existing similarity rule.

CREATE OR REPLACE FUNCTION core.form_d_trigram_name(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN translate(normalize(value, NFKD),
      U&'\0027\2019\02bc' ||
      U&'\0300\0301\0302\0303\0304\0305\0306\0307\0308\0309\030a\030b\030c\030d\030e\030f' ||
      U&'\0310\0311\0312\0313\0314\0315\0316\0317\0318\0319\031a\031b\031c\031d\031e\031f' ||
      U&'\0320\0321\0322\0323\0324\0325\0326\0327\0328\0329\032a\032b\032c\032d\032e\032f' ||
      U&'\0330\0331\0332\0333\0334\0335\0336\0337\0338\0339\033a\033b\033c\033d\033e\033f' ||
      U&'\0340\0341\0342\0343\0344\0345\0346\0347\0348\0349\034a\034b\034c\034d\034e\034f' ||
      U&'\0350\0351\0352\0353\0354\0355\0356\0357\0358\0359\035a\035b\035c\035d\035e\035f' ||
      U&'\0360\0361\0362\0363\0364\0365\0366\0367\0368\0369\036a\036b\036c\036d\036e\036f', '');

CREATE INDEX IF NOT EXISTS people_form_d_name_trgm_idx ON core.people
USING gin (core.form_d_trigram_name(full_name) gin_trgm_ops) WHERE merged_into_id IS NULL;
