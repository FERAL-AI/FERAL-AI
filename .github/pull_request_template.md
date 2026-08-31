## What this changes

<!-- The defect or the capability, not a restatement of the diff. If it
     is a fix, describe what was wrong and why it survived until now. -->

## Why this way

<!-- The alternative you rejected and the reason. Skip if there wasn't
     a real choice. -->

## How it was verified

<!-- Not "tests pass". What did you run, and how do you know the test
     would have failed before?

     The recurring defect class in this codebase is a seam nobody
     tested: two functions each individually correct, composed
     incorrectly. A test written after the fix that never saw the bug
     is worth very little, so demonstrating the failure first is the
     convention here. -->

- [ ] New tests fail against the unfixed code (say how you checked)
- [ ] `make test-py` green, or the subset plus a reason
- [ ] `make lint` green
- [ ] Behaviour change reflected in docs, README or CHANGELOG

## Anything you are unsure about

<!-- Genuinely useful. A reviewer who knows where you were uncertain
     reviews better than one told everything is fine. Say what you did
     not verify as well as what you did. -->
