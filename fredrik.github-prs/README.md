# GitHub pull requests

Shows your open pull requests and other authors' review-ready pull requests
from the configured repositories. Drafts and the configured label filters
remain excluded. Click a row to open its pull request, use arrow keys and
Enter to navigate, and press R to refresh.

Each row includes a review badge: **Approved**, **Changes requested**,
**Review required**, or **Awaiting review**. GitHub's aggregate review decision
takes precedence, including required reviews that are still outstanding.
When a repository has no aggregate decision, the latest opinionated review
per reviewer is used; changes requested take precedence over approvals.
An old cache without review metadata, or an incomplete review list without
an aggregate decision, displays **Review status unknown** until refreshed.

Native GitHub stacks display the PR's position and total stack size, plus
its immediate neighbors in base-to-top order. The current PR is shown in
brackets, for example `Stack 2/3 · #41 → [#42] → #43`. Hover over the row for
the stack's base branch and ordered PR titles. Stack entries are included
even when those PRs are excluded from the main list by author, draft, or
label filters. PRs without native stack metadata have no stack indicator;
branch names and PR descriptions are not used to guess stack membership.

Data is fetched using the authenticated `gh api graphql` command every five
minutes by default. Like the previous picker, each repository/category query
shows up to 30 matching PRs. Up to 100 reviews and 100 stack entries per PR
are fetched; truncated data is marked rather than treated as complete.
Successful refreshes are cached in
`${XDG_CACHE_HOME:-~/.cache}/fredrik-github-prs.json`. Failed refreshes show
an error and preserve the previous results.

Plugin settings: `myRepositories`, `reviewRepositories`, `filter`, and
`refreshMinutes`. This machine-local source is installed through the existing
`~/.config/omarchy/plugins/fredrik.github-prs` symlink.

Run the data and presentation helper tests with:

```sh
node --test ~/omarchy-plugins/fredrik.github-prs/test_pull_requests.cjs
```
