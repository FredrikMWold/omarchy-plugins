const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const helpers = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, "PullRequests.js"), "utf8"), helpers);

function reviews(states, hasNextPage = false) {
  return {
    latestOpinionatedReviews: {
      nodes: states.map(state => ({ state })),
      pageInfo: { hasNextPage },
    },
  };
}

function stacked(position = 2) {
  return {
    number: 40 + position,
    stackEntry: { position },
    stack: {
      number: 7,
      size: 3,
      baseRefName: "main",
      entries: {
        nodes: [3, 1, 2].map(position => ({
          position,
          pullRequest: {
            number: 40 + position,
            title: `Part ${position}`,
            url: `https://github.com/example/repo/pull/${40 + position}`,
          },
        })),
        pageInfo: { hasNextPage: false },
      },
    },
  };
}

test("query includes native stack metadata and current opinionated reviews", () => {
  const query = helpers.query();
  for (const field of ["reviewDecision", "latestOpinionatedReviews", "stackEntry",
    "position", "baseRefName", "hasNextPage"]) {
    assert.ok(query.includes(field), field);
  }
  assert.ok(query.includes("first: 30"));
});

test("parses search results and excludes drafts and null nodes", () => {
  const response = { data: { search: { nodes: [
    { number: 1, isDraft: false },
    { number: 2, isDraft: true },
    null,
    {},
  ] } } };
  const result = helpers.parseResponse(JSON.stringify(response));
  assert.equal(result.length, 1);
  assert.equal(result[0].number, 1);
});

test("rejects malformed responses and partial GraphQL errors", () => {
  for (const raw of ["", "[]", "{}", '{"data":{"search":null}}']) {
    assert.throws(() => helpers.parseResponse(raw));
  }
  assert.throws(() => helpers.parseResponse(JSON.stringify({
    data: { search: { nodes: [] } },
    errors: [{ message: "Resource not accessible" }],
  })), /Resource not accessible/);
});

test("GitHub aggregate decisions take priority over individual reviews", () => {
  for (const [reviewDecision, expected] of [
    ["APPROVED", "approved"],
    ["CHANGES_REQUESTED", "changes"],
    ["REVIEW_REQUIRED", "required"],
  ]) {
    assert.equal(helpers.reviewStatus({
      ...reviews(["APPROVED", "CHANGES_REQUESTED"], true),
      reviewDecision,
    }), expected);
  }
});

test("uses current opinionated reviews when branch rules give no decision", () => {
  assert.equal(helpers.reviewStatus(reviews(["APPROVED"])), "approved");
  assert.equal(helpers.reviewStatus(reviews(["APPROVED", "CHANGES_REQUESTED"])), "changes");
  assert.equal(helpers.reviewStatus(reviews(["COMMENTED", "DISMISSED", "PENDING"])), "awaiting");
  assert.equal(helpers.reviewStatus(reviews([])), "awaiting");
  assert.equal(helpers.reviewStatus({}), "unknown");
});

test("never infers approval from incomplete reviews", () => {
  assert.equal(helpers.reviewStatus(reviews(["APPROVED"], true)), "unknown");
  assert.equal(helpers.reviewStatus(reviews([], true)), "unknown");
  assert.equal(helpers.reviewStatus(reviews(["CHANGES_REQUESTED"], true)), "changes");
});

test("handles array-like Qt lists exposed by QML delegate modelData", () => {
  assert.equal(helpers.reviewStatus({
    latestOpinionatedReviews: { nodes: { 0: { state: "APPROVED" }, length: 1 } },
  }), "approved");
  assert.equal(helpers.reviewStatus({
    latestOpinionatedReviews: { nodes: { length: 0 } },
  }), "awaiting");
  const item = stacked();
  item.stack.entries.nodes = Object.assign({}, item.stack.entries.nodes, { length: 3 });
  assert.equal(helpers.stackLabel(item), "Stack 2/3 · #41 → [#42] → #43");
  assert.match(helpers.stackTooltip(item), /> 2\. #42 Part 2/);
});

test("every review state has a readable label", () => {
  for (const [status, label] of [
    ["approved", "Approved"],
    ["changes", "Changes requested"],
    ["required", "Review required"],
    ["awaiting", "Awaiting review"],
    ["unknown", "Review status unknown"],
  ]) {
    assert.equal(helpers.reviewLabel(status), label);
  }
});

test("stack labels show ordered neighbors and identify the current PR", () => {
  assert.equal(helpers.stackLabel(stacked(1)), "Stack 1/3 · main → [#41] → #42");
  assert.equal(helpers.stackLabel(stacked(2)), "Stack 2/3 · #41 → [#42] → #43");
  assert.equal(helpers.stackLabel(stacked(3)), "Stack 3/3 · #42 → [#43]");
  assert.equal(helpers.stackLabel({}), "");
  assert.equal(helpers.stackLabel({ stack: null }), "");
});

test("stack tooltips include the complete ordered stack and current marker", () => {
  assert.equal(helpers.stackTooltip(stacked()), [
    "Stack #7 · base: main",
    "  1. #41 Part 1",
    "> 2. #42 Part 2",
    "  3. #43 Part 3",
  ].join("\n"));
  assert.equal(helpers.stackTooltip({}), "");
});

test("missing or truncated stack entries do not invent neighbors or positions", () => {
  const item = stacked();
  item.stack.entries.nodes = [null, { position: 1, pullRequest: null }];
  item.stack.entries.pageInfo.hasNextPage = true;
  assert.equal(helpers.stackLabel(item), "Stack 2/3 · [#42]");
  assert.match(helpers.stackTooltip(item), /More stack entries on GitHub/);
  delete item.stackEntry;
  assert.equal(helpers.stackLabel(item), "Stack 3 · [#42]");
});
