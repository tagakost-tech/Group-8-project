# Incident playbook

For Omega IFS's operator (the person who runs the outreach agent) and Omega's
IT. One page per incident. Each page says who does what in the first hour,
then in the first day. Print it and keep it next to the machine that runs the
agent.

Who is who:

- **Operator**: the person at Omega who runs the agent and the dashboard.
- **IT**: Omega's IT, who runs the machine and the Microsoft 365 tenant.
- **Privacy contact**: the person named in `PRIVACY_CONTACT_EMAIL`.
- **Counsel**: Omega's lawyer for data protection.

Fill in before go-live, and keep it current:

| Role | Name | Phone |
| --- | --- | --- |
| Operator | | |
| IT | | |
| Privacy contact | | |
| Counsel | | |

Write down the time you noticed the problem, and keep notes of what you do
and when. You will need them for the Garante, for the people affected, or
for the review afterwards.

## Stopping the agent (all four incidents start here)

On the machine that runs the agent, in PowerShell, as the account that runs
it or as an administrator:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main\.py"?\s+(run|dashboard)\b' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

This stops both `main.py run` and `main.py dashboard` (the dashboard's Start
button runs the agent inside the dashboard, so stopping the dashboard stops
that agent too). Run the same line without the last part to check that
nothing is left; it must print nothing:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main\.py"?\s+(run|dashboard)\b' }
```

With Docker: `docker compose stop` in the project folder.

Also disable any scheduled task that starts the agent (`Disable-ScheduledTask
-TaskName "<its name>"`). The backup, retention, restore check and restore
drill tasks of `docs/DEPLOY.md` sections 8 and 9 do not send email: leave
them on, except during a restore (incident 4).

Stopping is safe. An email cut off half way is never sent again by itself
(`docs/DEPLOY.md` section 1).

While the dashboard is stopped, its unsubscribe page is offline too, so a
click on an unsubscribe link fails. Replies asking to stop still work: the
agent reads the inbox when it runs again, up to 14 days back. Where the
mailbox is safe (incident 2), start the dashboard again on its own as soon as
you can (`py -3.11 main.py dashboard`) and do not press Start.

---

## 1. Mailbox takeover suspected

Signs: emails in Sent Items that the agent did not send, replies to emails
nobody wrote, a Microsoft alert about a sign-in, bounces for emails Omega IFS
never sent, spam reports about Omega's domain.

Why it matters: whoever controls the mailbox can send spam or phishing as
Omega, to Omega's clients.

### First hour

| Who | What |
| --- | --- |
| Operator | Stop the agent and the dashboard (above). Tell IT and the privacy contact. Do not delete anything. |
| IT | Cut the agent's access to the mailbox. In Microsoft Entra, open the agent's app registration, **Certificates & secrets**, and delete its certificate, and any other certificate or client secret listed there (the agent uses one certificate and no secret, so anything else was added by someone else): no new sign-in with them works from then on. If there is any doubt, also **disable the app** (Enterprise applications, the app, Properties, "Enabled for users to sign in?" = No). If the mailbox still has a password anywhere, reset it and sign out all its sessions. |
| IT | A token Microsoft already gave out keeps working until it runs out, usually about an hour, so also close the mailbox itself to the app, in Exchange Online PowerShell (`docs/DEPLOY.md` section 12, steps 6 and 7, done in reverse): `Set-CASMailbox -Identity "<outreach mailbox>" -SmtpClientAuthenticationDisabled $true -ImapEnabled $false`, and `Remove-MailboxPermission -Identity "<outreach mailbox>" -User $sp.Identity -AccessRights FullAccess` (with `$sp` from step 6). Exchange can take a little while to apply them. Undo both only when the agent may run again. |
| IT | Check the sign-in logs in Entra for sign-ins you do not recognise: the **Service principal sign-ins** tab for the app (the agent signs in as the app), and the user sign-ins for the mailbox. Check Entra's audit log for certificates or secrets added to the app. |
| IT | Check **Sent Items** of each outreach mailbox, and Microsoft's **message trace** in the Exchange admin center (Mail flow, Message trace) for the last 7 days, for mail the agent did not send. The agent's own sends are in its email log (incident 2 shows how to list them). |
| IT | Check the mailbox for forwarding rules and inbox rules nobody made (Exchange admin center, the mailbox, Mail flow settings; and Outlook, Rules). Delete them. |

### First day

| Who | What |
| --- | --- |
| IT | Find how the attacker got in: a copied key file, the `.env`, a phished admin. If the machine itself was entered, treat it as incident 3 too. |
| IT | Make a new certificate on the agent's machine and upload it (`docs/DEPLOY.md` section 12, steps 1 and 3, and "Renewing the certificate"), and give the mailbox back to the app (steps 6 and 7). Run `py -3.11 main.py doctor --smtp` before the agent runs again. |
| Operator and privacy contact | If phishing or spam went out from Omega IFS's domain, **tell the people who got it**: what they received, that it was not from Omega, not to click its links or open its attachments, and who to call at Omega. Use the list from the message trace. |
| Privacy contact and counsel | If the attacker could read the mailbox, personal data was exposed (replies, opt-outs, lead alerts): decide whether it is a breach to notify, as in incident 3. |
| IT | Watch the domain's reputation for a week: bounces, spam reports, blocklists (`docs/DELIVERABILITY.md`). |

Start the agent again only once IT has found and closed the way in.

---

## 2. Wrong people emailed in bulk

Examples: Omega IFS's clients, competitors, people who had opted out, a
company that was blocked, people in a country not yet approved.

### First hour

| Who | What |
| --- | --- |
| Operator | Stop the agent. If it runs inside the dashboard, press **Stop** on the dashboard; otherwise stop it as above. Then start the dashboard on its own, so the unsubscribe links keep working, and do not press Start. |
| Operator | Write down when the wrong sending began, as close as you can. |
| Operator | List who was emailed since then. Run this in PowerShell in the project folder, with the start time in `SINCE`. It writes the list to `data\incident-sent.csv` (keep it there: it holds addresses) and says how many went to someone who had already opted out or whose company was blocked. It only reads the database. |

```powershell
@'
import csv, sqlite3
SINCE = "2026-11-16 09:00:00"  # when the wrong sending began
db = sqlite3.connect("file:data/agent.db?mode=ro", uri=True)
rows = db.execute(
    "SELECT e.timestamp, e.recipient_email, e.subject, e.delivery_status,"
    " e.sender_account,"
    " (SELECT s.reason FROM suppression_list s"
    "  WHERE s.email = LOWER(e.recipient_email) AND s.timestamp < e.timestamp),"
    " (SELECT d.reason FROM suppressed_domains d"
    "  WHERE d.domain = LOWER(SUBSTR(e.recipient_email, INSTR(e.recipient_email, '@') + 1))"
    "  AND d.timestamp < e.timestamp)"
    " FROM email_log e"
    " WHERE e.delivery_status IN ('sent', 'sent_unconfirmed') AND e.timestamp >= ?"
    " ORDER BY e.timestamp",
    (SINCE,),
).fetchall()
with open("data/incident-sent.csv", "w", newline="", encoding="utf-8") as f:
    out = csv.writer(f)
    out.writerow(["sent", "to", "subject", "status", "mailbox",
                  "already opted out or blocked", "company already blocked"])
    out.writerows(rows)
print(len(rows), "email(s) written to data/incident-sent.csv;",
      sum(1 for r in rows if r[5] or r[6]), "went to someone already stopped")
'@ | py -3.11 -
```

The dashboard's **Emails** tab shows the same emails, if you prefer to look
there. `sent_unconfirmed` means the email may have gone (the
send was cut off after the server took it).

| Who | What |
| --- | --- |
| Operator | Tell Omega IFS's sales lead and the privacy contact how many, and which kind (clients, competitors, opted-out people). |

### First day

| Who | What |
| --- | --- |
| Operator and sales | Go through `data\incident-sent.csv`. Mark the clients and competitors (compare with the client list and the competitor list). |
| Operator | Stop each wrong recipient from being emailed again: **Mark as client** on the dashboard's Contacts tab for a client (it goes on the client list); add a competitor to the competitor list file (`COMPETITOR_LIST_PATH`; when it is encrypted, as in `docs/DEPLOY.md` section 13, "Changing a list by hand"); **Block** for anyone else. A dashboard Block is written to the journal, like an opt-out, so a restore from an older backup keeps it (`docs/DEPLOY.md` section 9). |
| Sales lead | Send an apology to the people who should not have been emailed. Outline: one short email, from a named person, plain text; say the earlier email was sent by mistake; say they will not get further emails (and that this is done); for someone who had opted out, say their opt-out stands and you are sorry it was not respected; give a direct contact; no sales content. For clients, a phone call from their usual contact is better than an email. |
| Privacy contact and counsel | An email to someone who had opted out goes against their objection (GDPR Art. 21). Record it, and decide whether anything must be reported. Emailing the wrong person is not by itself a data breach, but a lead alert or reply that went to the wrong person is (incident 3). |
| Developer | Find why the screen let them through: a client list that did not load, a wrong country setting, a restore that lost opt-outs (run `py -3.11 main.py restore-drill`), a code fault. `py -3.11 main.py doctor` shows most of these. Fix it before the agent runs again. |

Start the agent again only once the cause is fixed and `doctor` shows no
`[FAIL]`.

---

## 3. Data leak, or a stolen or lost laptop

Signs: the machine that runs the agent is lost or stolen; someone copied
`data\`, a backup, the logs or `.env`; the dashboard was reached by someone
who should not have; malware on the machine.

### First hour

| Who | What |
| --- | --- |
| Operator | If the machine is still there, stop the agent and the dashboard (above). Do not wipe or reinstall anything yet. Tell IT and the privacy contact now. Write down the time. **The 72-hour clock for the Garante starts when Omega IFS becomes aware of the breach.** |
| IT | If the machine is lost: lock and, if possible, remotely wipe it. Check that its disk was encrypted (BitLocker): an encrypted disk with its key safe lowers the risk a great deal. |
| IT | Delete the agent's certificate in Microsoft Entra (incident 1, first hour). Whoever has the key file can send as Omega. |
| IT | Work out what was on the machine or copied. The list of what each store holds is in `docs/privacy/breach_procedure.md`, "Breaches this product could suffer": the database `data\agent.db`, `data\backups\` (the last 14 full copies), `data\journal.jsonl`, `data\exports\`, `data\spool\`, `logs\` and `.env`. Also any copy of `data\` or the journal kept elsewhere. |

### First day

| Who | What |
| --- | --- |
| Privacy contact and counsel | Follow `docs/privacy/breach_procedure.md`, "Steps": assess the risk to the people, decide on notifying the Garante within 72 hours, and on telling the people affected. Record it in its register. This page does not repeat that procedure. |
| IT | Change every secret in `.env` except `ERASURE_KEY`: the dashboard password (`DASHBOARD_PASSWORD`), the unsubscribe secret (`UNSUBSCRIBE_SECRET`: links already sent keep working, but a backup made before the change cannot be restored with all of them, since a restore needs the same secret; so run `py -3.11 main.py backup` right after the change and restore only from backups made since), the AI server's key (`LLM_API_KEY`) if one is set, any mailbox password still kept, and the Microsoft certificate (make a new one, `docs/DEPLOY.md` section 12). |
| IT | If the client and competitor lists were encrypted (`LISTS_KEY`) and the copy did not include `.env` or the key's backup, they were not readable. If it did, make a new `LISTS_KEY` and encrypt the lists again (`docs/DEPLOY.md` section 13, "If the key is lost or must change"). |
| IT | **Do not change `ERASURE_KEY`.** Every person erased on request is kept only as a code made with this key. The database refuses a different key, and with a new key nobody erased earlier would be recognised: the agent could find them again and email them. If the key leaked with the database, treat that as part of the breach (someone holding both can check whether a guessed address was erased); do not try to fix it by changing the key. |
| IT | Set the agent up again on a clean, encrypted machine. If the database is gone or cannot be trusted, restore it as in incident 4. |

---

## 4. A restore is needed

Signs: the database is damaged (`doctor` cannot open it, errors about a
"malformed" database), the disk failed, ransomware, or a mistake that wiped
data.

The danger: a backup is older than the database it replaces. Restored on its
own, it brings back everyone erased since it was made, forgets every
opt-out, company block, dashboard Block and bounce made since, and forgets
every email sent since, so the agent would send the same email to the same
people a second time. The journal
(`data\journal.jsonl`) puts them back. So the journal, and its copy on another disk, matter as much as
the backup.

### First hour

| Who | What |
| --- | --- |
| Operator | Stop the agent and the dashboard (above). |
| IT | Copy the whole `data\` folder as it is now to a safe place (an encrypted disk). It is your way back if the restore goes wrong, and evidence if it was an attack. |
| IT | Disable the backup, retention and restore drill tasks until the restore is verified (`Disable-ScheduledTask -TaskName "Omega outreach backup"`, and the same for "Omega outreach retention" and "Omega outreach restore drill"). Each backup deletes the oldest of the 14 copies and trims the journal to the oldest copy kept, so a backup taken of a damaged database, or before `replay-journal` has run, can cost you the copy you need and the journal lines it needs. (A backup of a restored database that has not been replayed yet keeps the whole journal, but it still deletes the oldest copy.) |
| IT | If the disk was lost, put the newest copy of the journal back as `data\journal.jsonl` first (`docs/DEPLOY.md` section 9). |
| IT | **Drill the backup before you restore it.** This restores it in a temporary folder, replays the journal, and checks that nothing is lost; it does not touch the live database: `py -3.11 main.py restore-drill --newest`, or for a copy from elsewhere `py -3.11 main.py restore-drill --backup <path to the backup file>`. It must end with `Restore drill PASSED`. If it fails, tell the developer. The report counts what is missing by kind (opt-outs, company blocks, address blocks, bounces, erasures, sends). Its "Sends" line says how many emails went out since the backup, for example "12 send(s) since the backup, all restored". Something missing means the journal did not record it, so an older backup fails the same way: the developer must decide with you whether to restore anyway and stop those people or companies again by hand. A missing send is an email the agent would send a second time. |

### Then restore

1. With the agent and the dashboard still stopped, copy the backup that
   passed the drill over `data\agent.db`, and delete `data\agent.db-wal` and
   `data\agent.db-shm` if they exist.
2. Run `py -3.11 main.py replay-journal` before anything else. It puts back
   every opt-out, company block and lift, dashboard Block and Unblock,
   bounce, erasure and data request recorded since the backup, in the order
   they happened. It also puts back every email sent since the backup, so no
   one gets the same first email or follow-up twice. Its last count,
   "sends", is the number of emails it put back. Running it a second time
   does no harm: it then reports 0 sends. It ends with "The agent may
   start." Until then the agent refuses to start: `main.py run`, the
   dashboard's Start and Direct Send say to run this command first.

### Then verify

1. `py -3.11 main.py restore-drill` (no option: it takes a fresh backup of the
   restored database and drills that). It must end with `Restore drill
   PASSED`.
2. `py -3.11 main.py doctor`: no `[FAIL]`.
3. Start the dashboard (`py -3.11 main.py dashboard`), look in its Live
   Activity panel for the `RESTORE_DRILL` line, and only then start the agent.

### First day

| Who | What |
| --- | --- |
| Operator | Remember what a restore cannot bring back (`docs/DEPLOY.md` section 9). Emails sent since the backup are put back by the replay: those contacts show as "emailed" and are not emailed again, and a follow-up that already went is not repeated. Emails sent before the agent was updated to journal them are not put back: if the backup is older than that update, those people can get the same email again. But a contact whose first email went after the backup gets no follow-ups from the agent any more (send one by hand if you want it), and replies recorded since the backup are gone from the dashboard: read the mailbox. Blocks, Unblocks and bounces since the backup are put back by the replay, and so is the contact's status ("blocked", "bounced") where the backup holds the contact. Look at the dashboard before pressing Start. |
| IT | Find out why the restore was needed. If it was an attack or a lost disk with data on it, it is also incident 3. |
| IT | Enable the tasks disabled in the first hour again (`Enable-ScheduledTask`), and check that the daily backup and the journal copy are running again (`docs/DEPLOY.md` section 9). |
