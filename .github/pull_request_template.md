<!-- markdownlint-disable MD041 -->
<!--
PULL REQUEST TITLE: this decides the release-notes section and the version bump. It appears word for word
in the release notes, so write it as a sentence a user would understand, after one of these keywords and a colon.
The title is not part of this description box; set it in the title field above.

  Keyword                                                       Section            Bump (from 1.4.2)
  feat:                                                         Features           minor  -> 1.5.0
  fix:   perf:                                                  Bug Fixes          minor  -> 1.5.0
  patch:  (a very minor bug fix)                                Bug Fixes          patch  -> 1.4.3
  chore:  build:  ci:  test:  refactor:  style:                 Maintenance        patch  -> 1.4.3
  docs:                                                         Documentation      patch  -> 1.4.3
  feat!:  fix!:  chore!:  or  breaking:                         Breaking Changes   major  -> 2.0.0

Optional scope: feat(modbus): ...   Example title: feat: Add a Changelog page under Global Settings
Dependabot titles ("chore(deps): ...") already follow this and need no template.
Add the label skip-changelog to keep this pull request out of the release notes.
These comments are not shown on the finished pull request, so you can leave them in.
-->

## Summary

<!-- What does this change and why? One to three sentences. -->

## Details

<!-- Anything a reviewer or a future reader needs: approach, trade-offs, files or options that were renamed. -->

## Testing

<!-- How you checked it: tests added or run, devices or protocols tried, screens clicked through. -->

## Checklist

- [ ] The title starts with one of the keywords above
- [ ] Tests added or updated, and `pytest` passes
- [ ] Strict typing, with no new Pylance or pyright errors, and `ruff` is clean
- [ ] Docstrings are thorough and follow the existing naming conventions
- [ ] Documentation affected by this change is updated
- [ ] American spelling is used

## Breaking change

- [ ] This is a breaking change

<!-- Tick the box above if existing settings, files, APIs or behavior stop working. Ticking it marks this
pull request as a major release. Then describe what breaks and how to migrate: -->

## Related issues

<!-- For example: Closes #123 -->
Closes #
