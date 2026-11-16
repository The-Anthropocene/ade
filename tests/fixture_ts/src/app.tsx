import React from "react";
import { add } from "@/lib";
import {
  twice,
} from "./util.js";
const lazy = () => import("./late");
const legacy = require("../scripts/helper.cjs");
export const App = () => add(twice(1), 2);
const joined = require(path.join(__dirname, "..", "scripts", "joined.cjs"));
const cfg = require("../data/cfg.json");
const ROOT = path.resolve(__dirname, '..');
const rooted = require(path.join(ROOT, 'scripts', 'rooted.cjs'));
const BAD = path.resolve(process.cwd(), '..');
const skipped = require(path.join(BAD, 'scripts', 'ignored.cjs'));
