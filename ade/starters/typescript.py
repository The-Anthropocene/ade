# id: ade.starters.typescript
# what: the TypeScript starter: one module with a header, one passing node:test test (Node 22.6+, no install), a first card
# cmd: -
# reads: -
# writes: -
# Templates use str.format: {pkg} is the package, {name} the folder; literal braces are doubled.

FILES = {
    "README.md": (
        "# {name}\n\nStarted with `ade`. Run the tests with `npm test` (Node 22.6 or newer, nothing to install).\n\n"
        "1. Run `ade loop c1`: the engine builds the first card, a review and the tests run, and it lands on trunk.\n"
        "2. Look with `ade cockpit`.\n"
        "3. Add your own change with `ade card add \"title\" --path <glob> --done \"when it is done\"`, "
        "or let the pilot draft one with `ade card draft \"one sentence\"`.\n"
        "4. `ade conduct` flies every open card.\n"
        "5. `ade` alone lists every verb.\n"),
    ".gitignore": ".ade/\nnode_modules/\ndist/\n",
    "package.json": (
        "{{\n  \"name\": \"{pkg}\",\n  \"private\": true,\n  \"type\": \"module\",\n"
        "  \"scripts\": {{ \"test\": \"node --experimental-strip-types --test src/*.test.ts\" }}\n}}\n"),
    "tsconfig.json": (
        "{{\n  \"compilerOptions\": {{\n    \"strict\": true,\n    \"module\": \"nodenext\",\n"
        "    \"allowImportingTsExtensions\": true,\n    \"noEmit\": true,\n    \"baseUrl\": \".\",\n"
        "    \"paths\": {{ \"@/*\": [\"./src/*\"] }}\n  }}\n}}\n"),
    "src/lib/README.md": "Shared helpers live here. The graph tells agents to reuse them before writing new ones.\n",
    "src/greet.ts": (
        "// id: {pkg}.greet\n// what: the starter's one function; the first card makes it take a name\n"
        "// cmd: -\n// reads: -\n// writes: -\n\nexport function greet(): string {{\n  return \"Hello, world!\";\n}}\n"),
    "src/greet.test.ts": (
        "// id: {pkg}.greet.test\n// what: covers src/greet.ts\n// cmd: node --experimental-strip-types --test src/greet.test.ts\n"
        "// reads: -\n// writes: -\nimport {{ test }} from \"node:test\";\nimport assert from \"node:assert/strict\";\n"
        "import {{ greet }} from \"./greet.ts\";\n\ntest(\"greets the world\", () => {{\n"
        "  assert.equal(greet(), \"Hello, world!\");\n}});\n"),
}

TOML = {'command = "{python} -m unittest {tests}"': 'command = "node --experimental-strip-types --test {test}"',
        'require = []': 'require = ["header"]',
        'blessed = []': 'blessed = "src/lib/*"'}

CARD = {"title": "greet takes a name", "paths": ["src/greet.ts", "src/greet.test.ts"],
        "done": "greet(\"Ada\") returns \"Hello, Ada!\", greet() still greets the world, and a test says both"}
