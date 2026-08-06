# IT Asset Management System — Process Architecture

- **Level 1** — one process flow per module (Assets, Workstations, Employees, Accountabilities, Audits).
- **Level 2** — a graph showing how the modules connect to and depend on each other.

Editable version: `process-architecture.drawio` (open in [diagrams.net](https://app.diagrams.net)).
The Mermaid diagrams below render on GitHub/GitLab/VS Code for quick review.

---

## Level 1 — Process per Module

### Assets

```mermaid
flowchart TD
  A1[Asset acquired / received] --> A2[Register asset<br/>tag, serial, type, model, status=Available]
  A2 --> A3{Assign or retire?}
  A3 -- assign --> A4[Assign to employee / workstation<br/>via Accountability or transfer<br/>status=Assigned, assigned_to set]
  A4 -- reassign --> A5[Transfer / reassign to another employee]
  A5 -.-> A4
  A4 -- return --> A6[Return / unlink<br/>status=Available] --> A8[Re-assignable / available]
  A8 -.-> A3
  A3 -- retire --> A7[Retire / archive<br/>decommission, remove from inventory]
```

### Workstations

```mermaid
flowchart TD
  W1[Register workstation<br/>code, name, location, status=Active] --> W2[Assign to employee<br/>batch transfer / accountability]
  W2 --> W3[Link assets to workstation<br/>assign-asset sets asset.workstation_id]
  W2 --> W4[Batch transfer to new employee<br/>auto-creates Accountability record]
  W3 --> W5[Unlink asset / workstation returned<br/>asset back to Available]
  W5 -.-> W3
  W2 --> W6[Archive workstation<br/>no longer active]
```

### Employees

```mermaid
flowchart TD
  E1[Add employee<br/>name, ID, department, position, email] --> E2[Active employee<br/>searchable, selectable in handovers]
  E2 --> E3[Update / edit details] -.-> E2
  E2 --> E4[Receive assets / workstation<br/>via Accountability record] --> E5[Return assets / close handover]
  E5 -.-> E2
  E2 --> E6[Archive employee<br/>status inactive, kept in trail]
```

### Accountabilities (Handover)

```mermaid
flowchart TD
  subgraph OFF["IT Officer / Admin"]
    A1[Create New Accountability<br/>select employee, workstation, assets] --> A2[Submit record<br/>type, effective date]
    A3[Close / return record<br/>when asset is handed back]
  end
  subgraph SYS["System (Flask + MongoDB)"]
    S1[Validate & save record<br/>status=Active, audit log] --> S2[Mark assets / workstation Assigned<br/>+ assign to employee] --> S3[Send receive-confirmation email<br/>tokenized link] --> S4[Track lifecycle<br/>Active → Received/Approved → Returned] --> S5[On close: status=Returned<br/>assets/workstation back to Available]
  end
  subgraph EMP["Employee"]
    E1[Receives email → opens link<br/>→ confirms receipt of assets]
  end
  A2 -. submit .-> S1
  S3 -. email .-> E1
  E1 -. confirm .-> S4
  A3 -. close .-> S5
```

### Audits

```mermaid
flowchart TD
  A1[Plan / schedule audit<br/>create audit record] --> A2[Physical count / scan assets<br/>vs system records] --> A3{Discrepancy found?}
  A3 -- yes --> A4[Document findings / action items<br/>reassign, transfer, correct records] --> A5[Generate audit report / PDF]
  A3 -- no --> A5
  A2 --> A6[Audit trail: every module action logged<br/>create/update/transfer/close/email]
  A6 -.-> A7[AI anomaly detection<br/>asset changes, expiring handovers]
```

---

## Level 2 — How the Modules Connect

```mermaid
flowchart LR
  EMP[EMPLOYEES<br/>master data] ---|assigned_to → employee<br/>asset held by employee| AST[ASSETS<br/>inventory]
  EMP ---|assigned_to → employee<br/>workstation assigned| WS[WORKSTATIONS]
  EMP ---|employee_id → employee<br/>record belongs to| ACC[ACCOUNTABILITIES<br/>handover records]
  ACC ---|asset_ids → assets<br/>marks them Assigned| AST
  ACC ---|workstation_id → workstation| WS
  AST ---|workstation_id / assets[]<br/>linked via assign-asset| WS
  AUD[AUDITS &amp; TRAIL] -.->|audit_log() on every action| EMP
  AUD -.->|audit_log() → trail| ACC
  AUD -.->|audit_log() → trail<br/>audit records reference assets| AST
  AUD -.->|audit_log() → trail| WS
```

### Shared data fields (which module owns what reference)

| From | To | Field / relationship | Triggered by |
|---|---|---|---|
| Assets | Employees | `assigned_to` → employee holding the asset | Accountability create, transfer, assign-asset |
| Workstations | Employees | `assigned_to` → employee holding the workstation | Batch transfer, accountability |
| Accountabilities | Employees | `employee_id` → owner of the record | Accountability create |
| Accountabilities | Assets | `asset_ids` → assets covered; status → `Assigned` | Accountability create (or link asset) |
| Accountabilities | Workstations | `workstation_id` → workstation covered | Accountability create |
| Assets | Workstations | `workstation_id` / `assets[]` → linked assets | `assign-asset`, `unlink-asset` |
| All modules | Audits | `audit_log()` → audit trail entries | Every create/update/transfer/close/email |
| Audits | Assets | audit records reference assets during physical count | Audit create |

### Cross-module actions that update MULTIPLE collections at once

1. **Creating an Accountability** → marks the selected assets **and** the workstation as `Assigned`, sets `assigned_to` on both, and records the audit log entry.
2. **Batch-transferring a workstation** → auto-creates an `Accountability` record (type "Workstation Transfer") for the new employee.
3. **Assigning an asset to a workstation** (`assign-asset`) → sets the asset's `workstation_id`, inherits the workstation's current holder as `assigned_to`, and adds the asset to the **active** Accountability if one exists.
4. **Unlinking an asset from a workstation** → asset back to `Available`, `assigned_to` cleared, and removed from the active Accountability's `asset_ids`.
5. **Closing an Accountability** → assets/workstation back to `Available`, cleared for the next handover; the timeline + audit trail are updated.

---

## Notation

- **Rounded rectangle** — process step / activity
- **Diamond** — decision / gateway
- **Swimlane** — actor or system responsible for the steps
- **Dashed arrow** — system/external handoff (email, API, auto-generated record)
- **Solid arrow** — sequential flow
