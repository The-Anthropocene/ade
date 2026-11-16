# id: ade.starters.python
# what: the Python starter: one module with a five-line header, one passing unittest, and a first card that grows it
# cmd: -
# reads: -
# writes: -
# Templates use str.format: {pkg} is the package, {name} the folder; literal braces are doubled.

FILES = {
    "README.md": (
        "# {name}\n\nStarted with `ade`. Run the tests with `python -m unittest`.\n\n"
        "1. Run `ade loop c1`: the engine builds the first card, a review and the tests run, and it lands on trunk.\n"
        "2. Look with `ade cockpit`.\n"
        "3. Add your own change with `ade card add \"title\" --path <glob> --done \"when it is done\"`, "
        "or let the pilot draft one with `ade card draft \"one sentence\"`.\n"
        "4. `ade conduct` flies every open card.\n"
        "5. `ade` alone lists every verb.\n"),
    ".gitignore": ".ade/\n__pycache__/\nbuild/\n*.egg-info/\n.venv/\n",
    "{pkg}/__init__.py": "# id: {pkg}\n# what: the {name} package\n# cmd: -\n# reads: -\n# writes: -\n",
    "{pkg}/greet.py": (
        "# id: {pkg}.greet\n# what: the starter's one function; the first card makes it take a name\n"
        "# cmd: -\n# reads: -\n# writes: -\n\n\ndef greet():\n    return \"Hello, world!\"\n"),
    "{pkg}/lib/README.md": "Shared helpers live here. The graph tells agents to reuse them before writing new ones.\n",
    "tests/__init__.py": "# id: tests\n# what: the test package\n# cmd: python -m unittest\n# reads: -\n# writes: -\n",
    "tests/test_greet.py": (
        "# id: tests.test_greet\n# what: covers {pkg}.greet\n# cmd: python -m unittest tests/test_greet.py\n"
        "# reads: -\n# writes: -\nimport unittest\n\nfrom {pkg}.greet import greet\n\n\n"
        "class GreetTest(unittest.TestCase):\n    def test_greets_the_world(self):\n"
        "        self.assertEqual(greet(), \"Hello, world!\")\n"),
}

TOML = {"require = []": "require = [\"header\"]",
        "blessed = []": 'blessed = "{pkg}/lib/*"'}

CARD = {"title": "greet takes a name", "paths": ["{pkg}/greet.py", "tests/test_greet.py"],
        "done": "greet(\"Ada\") returns \"Hello, Ada!\", greet() still greets the world, and a test says both"}
