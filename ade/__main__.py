# id: ade.main
# what: `python -m ade` entry, same as bin/ade
# cmd: python -m ade <verb> [args]
# reads: -
# writes: -
import sys

from ade.cli import main

sys.exit(main())
