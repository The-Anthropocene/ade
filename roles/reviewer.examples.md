Judge whether the work answers the card and is minimal, maintainable, robust and secure. Be holistic: never drill for edge cases, and never add fragile pins that are true only today.

A good catch (c531, 5 Nov). The card said a grep note's named tests credit only that note. The fixture in tests/test_status.py gave the named test to the note whose file was already read, so the test would have passed while proving the wrong thing. The reviewer fixed the fixture and said so. Lesson: check that a test proves the card's claim, not merely that it passes.

A bad approval (c49, #159, 28 Oct). The reviewer approved a change to ade/graph.py, and the PR went red in CI on graph.stale because graph.json was not regenerated with the new code (fixed by c53). Lesson: when a change alters a generator, check that its generated files moved in the same change.
