function names(values) {
  if (!values || typeof values.length !== "number" || typeof values === "string") {
    throw new Error("Repository selections must be lists")
  }
  var result = []
  var seen = {}
  for (var i = 0; i < values.length; i++) {
    if (typeof values[i] !== "string") throw new Error("Invalid repository name")
    var name = values[i].trim()
    if (!/^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/.test(name)
        || /\/\.\.?$/.test(name)) {
      throw new Error("Use an owner/repository name: " + name)
    }
    var key = name.toLowerCase()
    if (!seen[key]) {
      result.push(name)
      seen[key] = true
    }
  }
  return result
}

function selections(myRepositories, reviewRepositories) {
  return {
    version: 1,
    myRepositories: names(myRepositories),
    reviewRepositories: names(reviewRepositories)
  }
}

function parseSettings(raw) {
  var settings = JSON.parse(raw)
  if (!settings || settings.version !== 1
      || !Array.isArray(settings.myRepositories) || !Array.isArray(settings.reviewRepositories)) {
    throw new Error("Invalid repository settings")
  }
  return selections(settings.myRepositories, settings.reviewRepositories)
}

function includes(values, name) {
  var key = name.toLowerCase()
  for (var i = 0; i < values.length; i++) {
    if (String(values[i]).toLowerCase() === key) return true
  }
  return false
}

function toggle(values, name) {
  var result = []
  var selected = includes(values, name)
  for (var i = 0; i < values.length; i++) {
    if (String(values[i]).toLowerCase() !== name.toLowerCase()) result.push(values[i])
  }
  if (!selected) result.push(name)
  return names(result)
}

function rows(myRepositories, reviewRepositories, discovered) {
  var all = []
  var lists = [myRepositories, reviewRepositories, discovered]
  for (var i = 0; i < lists.length; i++) {
    for (var j = 0; j < lists[i].length; j++) all.push(lists[i][j])
  }
  return names(all).sort(function(a, b) {
    return a.toLowerCase().localeCompare(b.toLowerCase())
  })
}

function filterPullRequests(pullRequests, myRepositories, reviewRepositories) {
  var result = []
  for (var i = 0; i < pullRequests.length; i++) {
    var pr = pullRequests[i]
    if (includes(pr.type === "my" ? myRepositories : reviewRepositories, pr.repository || "")) {
      result.push(pr)
    }
  }
  return result
}

function discoveryCommand(search) {
  var query = search.trim()
  if (query === "") {
    return ["gh", "api", "graphql", "-f", "query={viewer{repositories(first:100,"
      + "orderBy:{field:UPDATED_AT,direction:DESC},"
      + "affiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER])"
      + "{nodes{nameWithOwner} pageInfo{hasNextPage}}}}"]
  }
  if (/^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/.test(query)) {
    return ["gh", "repo", "view", query, "--json", "nameWithOwner"]
  }
  return ["gh", "api", "graphql", "-f",
    "query=query($search:String!){search(query:$search,type:REPOSITORY,first:30)"
      + "{nodes{...on Repository{nameWithOwner}}pageInfo{hasNextPage}}}",
    "-f", "search=" + query]
}

function parseDiscovery(raw, search) {
  var response = JSON.parse(raw)
  var items
  var more = false
  if (response && response.errors && response.errors.length > 0) {
    throw new Error(response.errors.map(function(error) { return error.message }).join("; "))
  }
  if (response && response.data) {
    var repositories = search.trim() === ""
      ? response.data.viewer && response.data.viewer.repositories : response.data.search
    if (!repositories || !Array.isArray(repositories.nodes)
        || !repositories.pageInfo || typeof repositories.pageInfo.hasNextPage !== "boolean") {
      throw new Error("GitHub returned invalid repository data")
    }
    items = repositories.nodes
    more = repositories.pageInfo.hasNextPage
  } else if (response && typeof response.nameWithOwner === "string") {
    items = [response]
  } else {
    throw new Error("GitHub returned invalid repository data")
  }
  var found = []
  for (var i = 0; i < items.length; i++) {
    if (!items[i]) throw new Error("GitHub returned invalid repository data")
    found.push(items[i].nameWithOwner)
  }
  return { repositories: names(found), more: more }
}
