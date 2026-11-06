-- Optional target period for a falsifier. When set, only the indicator point for that period is tested
-- (a same-month-next-year test for seasonal series); when null, the latest point is tested on every run.
ALTER TABLE falsifiers ADD COLUMN period date;
