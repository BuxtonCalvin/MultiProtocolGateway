<!-- markdownlint-disable MD041 -->
<!--
PULL REQUEST TITLE: this decides the release-notes section and the version bump. Pull requests are squash-merged and
the title becomes the commit message the release notes are built from, so it appears word for word in them. Start it
with one of these keywords and a colon, then write a sentence a user would understand.
The title is not part of this description box; set it in the title field above.

  Keyword                                                       Section            Bump (from 1.4.2)
  feat:                                                         Features           minor  -> 1.5.0
  fix:   perf:                                                  Bug Fixes          minor  -> 1.5.0
  patch:  (a very minor bug fix)                                Bug Fixes          patch  -> 1.4.3
  chore:  build:  ci:  test:  refactor:  style:                 Maintenance        patch  -> 1.4.3
  docs:                                                         Documentation      patch  -> 1.4.3
  feat!:  fix!:  chore!:  or  breaking:                         Breaking Changes   major  -> 2.0.0

Optional scope: feat(modbus): ...   Example title: feat: Add a Changelog page under Global Settings
Add [skip changelog] to the title to leave a change out of the release notes.
A check on this pull request tells you if the title is missing a keyword. Dependabot titles ("chore(deps): ...")
already follow this and need no template.
These comments are not shown on the finished pull request, so you can leave them in.
-->

## Summary

<!-- What does this change and why? One to three sentences. -->

## Details

<!-- Anything a reviewer or a future reader needs: approach, trade-offs, files or options that were renamed. -->

## Testing

<!-- How you checked it: tests added or run, devices or protocols tried, screens clicked through. -->

## Checklist

- [ ] The title starts with one of the keywords above (add `!` before the colon if this breaks existing behavior)
- [ ] Tests added or updated, and `pytest` passes
- [ ] Strict typing, with no new Pylance or pyright errors, and `ruff` is clean
- [ ] Docstrings are thorough and follow the existing naming conventions
- [ ] Documentation affected by this change is updated
- [ ] American spelling is used

## Related issues

<!-- For example: Closes #123 -->
