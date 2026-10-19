const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const helpers = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, "Repositories.js"), "utf8"), helpers);
const plain = value => JSON.parse(JSON.stringify(value));

test("repository names are trimmed and deduplicated without changing casing", () => {
  assert.deepEqual(plain(helpers.names([" Owner/Repo ", "owner/repo", "other/repo"])),
    ["Owner/Repo", "other/repo"]);
  assert.deepEqual(plain(helpers.names({ 0: "owner/repo", length: 1 })), ["owner/repo"]);
});

test("invalid names and invalid list types cannot be saved", () => {
  for (const input of [null, {}, "owner/repo", ["repo"], ["https://github.com/owner/repo"],
    ["owner/repo is:open"], ["owner/.."], ["owner/."], [42]]) {
    assert.throws(() => helpers.names(input));
  }
});

test("saved settings round-trip both independent categories, including empty lists", () => {
  const settings = helpers.selections(["owner/mine"], ["owner/review"]);
  assert.deepEqual(plain(helpers.parseSettings(JSON.stringify(settings))), {
    version: 1, myRepositories: ["owner/mine"], reviewRepositories: ["owner/review"],
  });
  assert.deepEqual(plain(helpers.parseSettings(JSON.stringify(helpers.selections([], [])))), {
    version: 1, myRepositories: [], reviewRepositories: [],
  });
});

test("corrupt or unsupported settings are not silently accepted", () => {
  for (const input of ["", "null", "{}", '{"version":2,"myRepositories":[],"reviewRepositories":[]}',
    '{"version":1,"myRepositories":[]}', '{"version":1,"myRepositories":"owner/repo","reviewRepositories":[]}']) {
    assert.throws(() => helpers.parseSettings(input));
  }
});

test("repository selections toggle case-insensitively without mutating the draft", () => {
  const source = ["owner/one", "owner/two"];
  assert.deepEqual(plain(helpers.toggle(source, "OWNER/ONE")), ["owner/two"]);
  assert.deepEqual(plain(helpers.toggle(source, "owner/three")), ["owner/one", "owner/two", "owner/three"]);
  assert.deepEqual(source, ["owner/one", "owner/two"]);
});

test("discovery never hides selections absent from GitHub results", () => {
  assert.deepEqual(plain(helpers.rows(["owner/selected"], ["owner/review"],
    ["owner/found", "OWNER/SELECTED"])), ["owner/found", "owner/review", "owner/selected"]);
});

test("removed repositories disappear immediately in the correct PR category", () => {
  const prs = [
    { number: 1, type: "my", repository: "owner/one" },
    { number: 2, type: "review", repository: "OWNER/ONE" },
    { number: 3, type: "review", repository: "owner/two" },
  ];
  assert.deepEqual(plain(helpers.filterPullRequests(prs, [], ["owner/one"])), [prs[1]]);
  assert.deepEqual(plain(helpers.filterPullRequests(prs, [], [])), []);
});

test("repository discovery uses recent accessible repos, exact lookup, or safe search arguments", () => {
  const recent = plain(helpers.discoveryCommand(""));
  assert.deepEqual(recent.slice(0, 4), ["gh", "api", "graphql", "-f"]);
  assert.match(recent[4], /affiliations:\[OWNER,COLLABORATOR,ORGANIZATION_MEMBER\]/);
  assert.deepEqual(plain(helpers.discoveryCommand(" owner/repo ")),
    ["gh", "repo", "view", "owner/repo", "--json", "nameWithOwner"]);
  assert.deepEqual(plain(helpers.discoveryCommand("-topic:ignored org:owner")),
    ["gh", "api", "graphql", "-f",
      "query=query($search:String!){search(query:$search,type:REPOSITORY,first:30)"
        + "{nodes{...on Repository{nameWithOwner}}pageInfo{hasNextPage}}}",
      "-f", "search=-topic:ignored org:owner"]);
});

test("parses recent repositories and marks incomplete listings", () => {
  const raw = JSON.stringify({ data: { viewer: { repositories: {
    nodes: [{ nameWithOwner: "owner/one" }],
    pageInfo: { hasNextPage: true },
  } } } });
  assert.deepEqual(plain(helpers.parseDiscovery(raw, "")),
    { repositories: ["owner/one"], more: true });
});

test("parses exact lookup, search matches, and empty search results", () => {
  assert.deepEqual(plain(helpers.parseDiscovery('{"nameWithOwner":"owner/repo"}', "owner/repo")),
    { repositories: ["owner/repo"], more: false });
  assert.deepEqual(plain(helpers.parseDiscovery(
    '{"data":{"search":{"nodes":[{"nameWithOwner":"owner/repo"}],"pageInfo":{"hasNextPage":false}}}}', "repo")),
    { repositories: ["owner/repo"], more: false });
  assert.deepEqual(plain(helpers.parseDiscovery(
    '{"data":{"search":{"nodes":[],"pageInfo":{"hasNextPage":false}}}}', "missing")),
    { repositories: [], more: false });
});

test("rejects malformed repository responses and partial GraphQL errors", () => {
  for (const [raw, query] of [["{}", ""], ["[]", ""], ["{}", "repo"],
    ["[null]", "repo"], ['[{"fullName":"invalid"}]', "repo"],
    ['{"data":{"search":{"nodes":[]}}}', "repo"]]) {
    assert.throws(() => helpers.parseDiscovery(raw, query));
  }
  assert.throws(() => helpers.parseDiscovery(JSON.stringify({
    errors: [{ message: "Authentication required" }],
    data: { viewer: { repositories: { nodes: [] } } },
  }), ""), /Authentication required/);
});
