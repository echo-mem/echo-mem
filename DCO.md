# Developer Certificate of Origin

Contributions to Echo Memory are made under the **Developer Certificate of
Origin 1.1**, the same mechanism the Linux kernel, Docker and GitLab use.

There is nothing to read and agree to beyond the text below, no account to
create and no bot to reply to. You add one line to each commit:

```bash
git commit -s -m "your message"
```

which appends:

```
Signed-off-by: Your Name <your@email.address>
```

Forgot on a commit you already made:

```bash
git commit --amend -s --no-edit && git push --force-with-lease
```

On a branch with several:

```bash
git rebase --signoff main && git push --force-with-lease
```

The name and address must be real and must be yours; that is the whole point
of the certificate. A CI check reads the trailer on every commit in a pull
request and says so when one is missing.

## What this replaced, and why

Until 2026-10-26 this project asked for a Contributor License Agreement. It was
the Apache ICLA with the names substituted, and the one thing it added beyond
the Apache 2.0 licence was the right to distribute a contribution under
different terms later.

That right was dropped deliberately. Everything commercial in this project —
metering, billing, tenancy, roles — lives in a separate private repository and
always has, so the engine has never needed it. Asking every contributor to
grant a right that the project does not use, in a document most people will not
read, is friction bought with nothing.

The consequence is real and worth stating plainly rather than discovering
later: **no contributed code in this repository can be relicensed away from
Apache 2.0 without asking its author.** That is a constraint accepted on
purpose, not an oversight.

## The certificate

By making a contribution to this project, I certify that:

**(a)** The contribution was created in whole or in part by me and I have the
right to submit it under the open source licence indicated in the file; or

**(b)** The contribution is based upon previous work that, to the best of my
knowledge, is covered under an appropriate open source licence and I have the
right under that licence to submit that work with modifications, whether
created in whole or in part by me, under the same open source licence (unless
I am permitted to submit under a different licence), as indicated in the file;
or

**(c)** The contribution was provided directly to me by some other person who
certified (a), (b) or (c) and I have not modified it.

**(d)** I understand and agree that this project and the contribution are
public and that a record of the contribution (including all personal
information I submit with it, including my sign-off) is maintained
indefinitely and may be redistributed consistent with this project or the open
source licence(s) involved.

---

The DCO is reproduced above in full. The canonical text is at
<https://developercertificate.org/>.
