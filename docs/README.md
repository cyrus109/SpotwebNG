# Documentation assets

## Screenshots

`docs/img/` is **gitignored on purpose**. Screenshots are generated from a live instance,
and a live instance indexes whatever the Spotnet community posts — including adult
content.

Before adding or replacing a screenshot:

1. Confirm **Settings → Display → Hide adult content** is on (it is the default, and only
   an administrator can turn it off).
2. **Look at the image**, not just the setting. Categorisation comes from whoever posted
   the spot, so a mislabelled item can slip past the filter.
3. Add it explicitly with `git add -f docs/img/<name>.png`.

Never generate README images from an instance with the filter disabled.
