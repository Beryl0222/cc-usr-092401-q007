"use strict";

const { spawnSync } = require("node:child_process");

const result = spawnSync(
  "python3",
  ["-m", "unittest", "-v",
   "service_contract", "test_reward_center", "test_http_flows"],
  { stdio: "inherit" }
);
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
