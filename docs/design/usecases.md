---
type: usecases
project: code-server-spawner
---

# Use Cases

## UC-001: Student Logs In with Username and Password or Class Code

**Actor:** Student
**Preconditions:** Student account exists; student is enrolled in at least one class.

**Main Flow:**
1. Student navigates to `/auth/login`.
2. Student enters username and password (or a class code they are enrolled in).
3. System looks up the user by username.
4. System validates: password matches OR the supplied string is the `class_code`
   of a class the student is enrolled in.
5. System creates a session and redirects to the index page.

**Postconditions:** Student is authenticated and sees their class list.

**Error Flows:**
- Unknown username, wrong password, or non-enrolled class code: flash error,
  remain on login page.

---

## UC-002: Student Registers via Class Code (Google)

**Actor:** Student
**Preconditions:** A class with the given code exists and is within its date range.

**Main Flow:**
1. Student navigates to `/auth/register` and enters the class code.
2. System stores the class code in the session and redirects to Google OAuth.
3. Student authenticates with Google.
4. On callback, system creates a User record (role=student) and enrolls the
   student in the class identified by the stored class code.
5. Student is logged in and redirected to the index.

**Postconditions:** Student account exists; student is enrolled in the class.

**Error Flows:**
- Invalid class code: registration form validation error.
- Google auth fails or is denied: OAuth error page.

---

## UC-003: Student or Instructor Starts a Code-Server Host

**Actor:** Student or Instructor
**Preconditions:** A class is in `running` state; user is enrolled/assigned to
the class; user has no existing active host.

**Main Flow:**
1. User clicks "Start" for a class on the index page.
2. System forks the class's GitHub repo into the League-Students org for
   the student.
3. System creates a Docker Swarm service using the proto's image URI.
4. Docker's spread scheduler assigns the service to an available worker node.
5. A Caddy reverse-proxy entry is created with a per-host generated password.
6. A `CodeHost` record is created with `app_state=starting`.
7. Browser polls `/host/is_ready` until `app_state==ready`.
8. Browser redirects the user to the host's public URL.

**Postconditions:** A code-server container is running; student has a browser
IDE pre-loaded with the class curriculum repo.

**Error Flows:**
- GitHub fork fails: error returned; no host created.
- No available swarm nodes: Docker service creation fails; error shown.
- Host does not become ready within timeout: user sees a timeout/error message.

---

## UC-004: Host is Distributed Across Swarm Nodes

**Actor:** System (Docker Swarm scheduler)
**Preconditions:** Multiple worker nodes are registered in the swarm.

**Main Flow:**
1. When a Swarm service is created (UC-003, step 3), Docker applies its default
   spread scheduling strategy.
2. Docker selects the worker node with the lowest number of running tasks.
3. The container is started on that node; `CodeHost.node_id` and `node_name`
   are updated once the container is running.

**Postconditions:** Hosts are spread across available nodes; no single node
is overloaded disproportionately.

**Error Flows:**
- All nodes unavailable: service creation fails; surfaces as error in UC-003.

---

## UC-005: Instructor Views Class Roster

**Actor:** Instructor
**Preconditions:** Instructor is logged in and assigned to at least one class.

**Main Flow:**
1. Instructor navigates to the class detail page.
2. System queries enrolled students and their CodeHost records.
3. Page displays each student, their host state (waiting/starting/running/other),
   heartbeat age, and utilization metrics.
4. Instructor can stop any student's host from this view.

**Postconditions:** Instructor has a real-time view of class host states.

**Error Flows:**
- No students enrolled: empty roster is shown.

---

## UC-006: Operator Adds a Swarm Node via CLI

**Actor:** Admin / Operator
**Preconditions:** DigitalOcean token, SSH key, and swarm manager config are
present in the environment; Docker is reachable on the manager.

**Main Flow:**
1. Operator runs `cspawnctl node expand`.
2. CLI computes the next serial number from existing swarm node names.
3. CLI creates a new DigitalOcean droplet with the configured region, size,
   and image; assigns it to the project and attaches the DO tag.
4. CLI waits for the droplet to become active and SSH-accessible.
5. CLI SSHes in and sets the hostname to the computed short name.
6. CLI joins the node to the Docker Swarm as a worker.
7. CLI applies the configured swarm node label.
8. CLI syncs DNS A records for the domain.

**Postconditions:** A new worker node appears in `docker node ls`; it is
immediately eligible to receive new host services.

**Error Flows:**
- DO token lacks permissions: descriptive error, droplet not created.
- SSH unreachable within timeout: `TimeoutError`; operator retries.
- Docker major version mismatch between manager and new node: error before
  join; operator must align Docker versions.

---

## UC-007: Operator Runs a Load Test

**Actor:** Admin / Operator
**Preconditions:** The app is configured and reachable; a Python Apprentice
ClassProto exists or can be created.

**Main Flow:**
1. Operator runs `cspawnctl test setup` — creates a load-test class and 20
   test student accounts enrolled in it.
2. Operator runs `cspawnctl test start` — starts code-server hosts for all 20
   students in parallel; records per-host start times.
3. Operator runs `cspawnctl test report` — prints timing distribution and
   host state summary.
4. Operator runs `cspawnctl test teardown` — stops all hosts, deletes GitHub
   forks, removes test students and the class.

**Postconditions:** No test artifacts remain in the database or on GitHub;
timing data was reported to the operator.

**Error Flows:**
- Individual host fails to start: recorded as a failure in the report; teardown
  still removes the others.
- GitHub fork deletion fails: logged as a warning; teardown continues.
