# Role taxonomy

A curated, data-only map of career **fields** to their **sub-roles** and **title synonyms**, so a user picks
a field (e.g. Cybersecurity) once and the filter catches every title variant and sub-role without listing
them by hand. Lives in `src/jobapply/roles/taxonomy.py`; pure Python, no DB, no LLM.

## How a user picks

On **Settings -> Filters** the "Career fields" picker shows one collapsible `<details>` per field. Each
field offers **Any `<field>` role** (covers the whole field) plus a checkbox for every sub-role. Selections
save to `filters.role_selection` as keys:

| Key form | Meaning |
|---|---|
| `cybersecurity` | the whole field (all sub-roles) |
| `cybersecurity:*` | the whole field (explicit form the UI saves) |
| `cybersecurity:soc` | one sub-role |

Free-text **Roles** and **Keywords** keep working alongside the picker; either the free-text role gate or a
taxonomy match satisfies the role check.

## How it feeds matching and scoring

- `match()` (`pipeline/filters.py`): when `role_selection` is set, a job passes the role gate if its title
  matches any selected sub-role via `match_title()` (word-boundary phrase matching), honouring each field's
  `exclude` list (so "Security Guard" never reads as Cybersecurity). `assign_jobs()` uses the same `match()`.
- Heuristic scorer (no-LLM path): on serialisation `UserFilters` folds `titles_for(role_selection)` into the
  stored `keywords_include` (the user's own text is preserved under `keywords_include_text` and restored on
  load), so `score_job_heuristic` — which reads the stored filters dict — treats the synonyms as keyword
  hits. Everything works with **no LLM key**; the taxonomy is pure data.

## Helpers (`jobapply.roles`)

- `all_fields()` -> list[Field]; `field(key)` -> Field | None; `subrole("field:sub")` -> SubRole | None
- `titles_for(selection)` -> set[str]: union of title keywords a selection expands to
- `match_title(title, selection)` -> bool: word-boundary match of those keywords against a job title,
  vetoed by the field's `exclude` list

## Fields and sub-roles

### Cybersecurity - `cybersecurity`

- **SOC analyst** (`cybersecurity:soc`): soc analyst, soc, security operations, security operations center, security analyst, soc engineer, ...
- **Threat intelligence** (`cybersecurity:threat_intel`): threat intelligence, cyber threat intelligence, cti, threat intel, threat intelligence analyst, threat researcher, ...
- **Incident response / DFIR** (`cybersecurity:incident_response`): incident response, incident responder, dfir, digital forensics, forensics, forensic analyst, ...
- **Penetration testing / red team** (`cybersecurity:pentest`): penetration testing, penetration tester, penetration, pentester, pen test, pen tester, ...
- **Blue team** (`cybersecurity:blue_team`): blue team, blue teamer, defensive security, cyber defense, cyber defence, security defense analyst
- **GRC / compliance** (`cybersecurity:grc`): grc, governance risk and compliance, governance risk compliance, security compliance, compliance analyst, cybersecurity compliance, ...
- **Application / product security** (`cybersecurity:appsec`): application security, product security, appsec, application security engineer, product security engineer, secure code, ...
- **Cloud security** (`cybersecurity:cloud_security`): cloud security, cloud security engineer, cloud security architect, cspm, aws security, azure security, ...
- **Security engineering** (`cybersecurity:security_engineering`): security engineer, security engineering, information security engineer, infosec engineer, cyber security engineer, cybersecurity engineer, ...
- **Malware analysis** (`cybersecurity:malware_analysis`): malware analyst, malware analysis, malware researcher, reverse engineer, reverse engineering, malware reverse engineer
- **Vulnerability management** (`cybersecurity:vuln_management`): vulnerability management, vulnerability analyst, vulnerability assessment, vuln management, vulnerability engineer, patch management, ...
- **Identity & access management** (`cybersecurity:iam`): identity and access management, iam, iam engineer, identity engineer, access management, identity and access, ...
- **OSINT** (`cybersecurity:osint`): osint, open source intelligence, open-source intelligence, osint analyst, open source investigator
- **Detection engineering** (`cybersecurity:detection_engineering`): detection engineering, detection engineer, detection and response, siem engineer, siem analyst, security content engineer, ...
- **Security architecture** (`cybersecurity:security_architecture`): security architect, security architecture, cybersecurity architect, cyber security architect, enterprise security architect, information security architect
- _excluded from this field:_ physical security, security guard, security officer, building security, loss prevention, national security guard, security supervisor

### Software Engineering - `software_engineering`

- **Software engineer (general)** (`software_engineering:software`): software engineer, software developer, sde, swe, programmer, software development engineer
- **Backend** (`software_engineering:backend`): backend engineer, backend developer, back-end engineer, back-end developer, backend software engineer, server-side engineer, ...
- **Frontend** (`software_engineering:frontend`): frontend engineer, frontend developer, front-end engineer, front-end developer, ui engineer, react developer, ...
- **Full-stack** (`software_engineering:fullstack`): full stack, full-stack, fullstack engineer, full stack developer, full stack engineer, full-stack developer
- **Mobile (iOS / Android)** (`software_engineering:mobile`): mobile developer, mobile engineer, ios developer, ios engineer, android developer, android engineer, ...
- **Embedded** (`software_engineering:embedded`): embedded engineer, embedded software, embedded systems, firmware engineer, firmware developer, embedded developer
- **Platform** (`software_engineering:platform`): platform engineer, platform developer, developer platform engineer, developer experience
- **API** (`software_engineering:api`): api engineer, api developer, integration engineer, integrations engineer

### DevOps / SRE / Infrastructure - `devops`

- **DevOps** (`devops:devops`): devops engineer, devops, ci/cd engineer, release engineer, build engineer
- **Site reliability** (`devops:sre`): site reliability engineer, sre, reliability engineer, production engineer
- **Infrastructure** (`devops:infra`): infrastructure engineer, infrastructure, systems engineer, cloud infrastructure engineer, infra engineer

### Data & AI - `data`

- **Data engineer** (`data:data_engineer`): data engineer, data engineering, etl developer, big data engineer, analytics engineer, data platform engineer
- **Data scientist** (`data:data_scientist`): data scientist, data science, applied scientist
- **Data / business analyst** (`data:data_analyst`): data analyst, business analyst, bi analyst, business intelligence, analytics analyst, reporting analyst
- **ML / AI engineer** (`data:ml_engineer`): machine learning engineer, ml engineer, ai engineer, deep learning engineer, nlp engineer, computer vision engineer, ...
- **MLOps** (`data:mlops`): mlops engineer, mlops, ml platform engineer, ml infrastructure, machine learning platform

### Product Management - `product`

- **Product manager** (`product:pm`): product manager, product management, technical product manager, tpm, group product manager, senior product manager
- **Product owner** (`product:product_owner`): product owner, po
- **Product analyst** (`product:product_analyst`): product analyst, product operations

### Design - `design`

- **UX / UI** (`design:ux_ui`): ux designer, ui designer, ux/ui designer, ui/ux designer, user experience, user interface designer, ...
- **Product design** (`design:product_design`): product designer, product design, senior product designer
- **UX research / visual** (`design:research_visual`): ux researcher, user researcher, interaction designer, visual designer, design researcher

### QA / Test Engineering - `qa`

- **QA engineer** (`qa:qa`): qa engineer, quality assurance, qa analyst, test engineer, qa tester, manual tester, ...
- **SDET / automation** (`qa:sdet`): sdet, software development engineer in test, automation engineer, test automation, automation tester

### IT Support / Sysadmin / Network - `it_ops`

- **IT support** (`it_ops:support`): it support, help desk, helpdesk, desktop support, technical support, service desk, ...
- **Sysadmin** (`it_ops:sysadmin`): system administrator, sysadmin, systems administrator, it administrator, windows administrator, linux administrator
- **Network** (`it_ops:network`): network engineer, network administrator, network admin, noc engineer, network operations

### Cloud Engineering - `cloud`

- **AWS** (`cloud:aws`): aws engineer, aws cloud engineer, aws solutions architect, amazon web services, aws developer
- **Azure** (`cloud:azure`): azure engineer, azure cloud engineer, azure administrator, azure developer, azure solutions architect
- **GCP** (`cloud:gcp`): gcp engineer, google cloud engineer, gcp cloud, google cloud platform, gcp developer
- **Cloud engineer / architect** (`cloud:cloud_architect`): cloud architect, cloud engineer, solutions architect, cloud consultant, cloud solutions architect

### Project / Program Management - `program`

- **Project manager** (`program:project_manager`): project manager, pmp, project management, it project manager
- **Program manager** (`program:program_manager`): program manager, technical program manager, tpm, delivery manager
- **Scrum / agile** (`program:scrum`): scrum master, agile coach, release train engineer

### Sales - `sales`

- **SDR / BDR** (`sales:sdr`): sales development representative, sdr, bdr, business development representative, lead generation
- **Account executive** (`sales:ae`): account executive, sales executive, enterprise sales, sales representative, inside sales
- **Sales / solutions engineer** (`sales:sales_eng`): sales engineer, solutions engineer, pre-sales, presales, solutions consultant
- **Account manager** (`sales:account_mgr`): account manager, key account manager, strategic account manager

### Marketing - `marketing`

- **Digital / growth** (`marketing:digital`): digital marketing, seo, sem, performance marketing, growth marketing, paid media, ...
- **Content** (`marketing:content`): content marketing, content writer, copywriter, content strategist, content manager
- **Product marketing** (`marketing:product_marketing`): product marketing, pmm, product marketing manager
- **Social / community** (`marketing:social`): social media, social media manager, community manager

### Finance & Accounting - `finance`

- **Accountant** (`finance:accountant`): accountant, accounting, staff accountant, accounts payable, accounts receivable
- **Financial analyst** (`finance:financial_analyst`): financial analyst, fp&a, finance analyst, fpa analyst
- **Controller** (`finance:controller`): controller, financial controller, finance controller

### HR / People - `hr`

- **Recruiting** (`hr:recruiter`): recruiter, talent acquisition, technical recruiter, sourcer, recruiting coordinator
- **HR generalist / ops** (`hr:hr_generalist`): hr generalist, human resources, hr manager, people operations, hrbp, hr business partner

### Customer Success & Support - `customer`

- **Customer success** (`customer:csm`): customer success, customer success manager, csm, customer success engineer
- **Customer support** (`customer:support_agent`): customer support, support specialist, customer service, customer care
