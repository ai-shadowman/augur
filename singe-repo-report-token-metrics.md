# Migration Report
- https://github.com/kfrankli/northpolesouthern-train.git

---
### Dependency Graph

```mermaid
graph TD
    mod_maven[Module: train (Maven)] --> mod_src[Module: src/main/java/com/northpolesouthern/train]
    mod_src --> pkg_train[Package: com.northpolesouthern.train]

    pkg_train --> class_Train[Class: Train]
    pkg_train --> class_Catalog[Class: TrainCatalog]
    pkg_train --> class_App[Class: TrainApp]

    class_Train --> func_ctor[Train::ctor]
    class_Train --> func_getName[Train::getName]
    class_Train --> func_getDepStation[Train::getDepartureStation]
    class_Train --> func_getDepTime[Train::getDepartureTime]
    class_Train --> func_getDestStation[Train::getDestinationStation]
    class_Train --> func_getDestTime[Train::getDestinationTime]
    class_Train --> func_toString[Train::toString]

    class_Catalog --> func_getTrains[TrainCatalog::getTrains]
    class_App --> func_main[TrainApp::main]

    func_getTrains --> class_Train
    func_main --> func_getTrains
    func_main --> class_Train
```

### High-Level Summary

## Overview  

The project is a **Java Maven application** that models the *North Pole Southern Train* domain. It defines a `Train` data class, supplies an immutable list of predefined `Train` objects through a static `TrainCatalog`, and includes a `TrainApp` class whose `main` method prints the catalog to the console. [Data: Reports (3, 2, 5, 4, 10, +more)]

## Architecture  

- **Module layout** – a single Maven module named **TRAIN** located under the package `com.northpolesouthern.train`.  
- **Source structure** – follows the conventional Maven layout (`src/main/java`).  
- **Components** –  
  * `TrainCatalog` uses a private constructor and a static `getTrains()` method to expose the train list, making the catalog immutable and self‑contained.  
  * `TrainApp` serves as the entry point, directly invoking the catalog and printing its contents.  
- The design is **tightly‑coupled but self‑contained**, requiring no external services. [Data: Reports (2, 3, 10, 11, 15, +more)]

## Key Dependencies  

The code relies exclusively on the **Java Standard Library**:  

- `java.time` – `LocalDateTime`, `DateTimeFormatter`  
- `java.util` – collections, arrays, `Objects`  
- `java.lang` – `String`, `System`  

Build‑time tooling is provided by **Apache Maven**; no third‑party libraries or external services are referenced. [Data: Reports (3, 2, 5, 4, 7, +more)]

## Runtime Stack  

- **Language / Platform** – Java **JDK 1.8**, as declared in the `pom.xml`.  
- **Build & Packaging** – Apache Maven with the `maven‑compiler‑plugin` and `maven‑jar‑plugin`.  
- **Infrastructure** – The application does **not** use databases, middleware, or additional frameworks. [Data: Reports (2, 14, 13, +more)]

---  

*The summary consolidates the most relevant information from the dataset while preserving the original references.*

### Recommended Migration Order

## Recommended Migration Order for a Low‑Impact Refactor  

When the goal is to minimise breaking changes, the migration should progress from the most isolated artefacts toward the central data model. The sequence below follows the evidence‑based priorities and keeps dependencies stable at each step.

| Step | What to migrate / change | Rationale (why it is safe first) | Key data references |
|------|--------------------------|----------------------------------|----------------------|
| **1. Maven build configuration** | Update `pom.xml`, Maven tool declaration, and plugin settings (`maven‑compiler‑plugin`, `maven‑jar‑plugin`). | Build files have **no runtime coupling** with the Java code, so they can be altered without affecting existing behaviour. | [Data: Reports (14, 10, 5, 4, +more)] |
| **2. Package and module definitions** | Rename the base package to `com.northpolesouthern.train` and adjust the `TRAIN` Maven module metadata. | This step groups the classes that will be moved later under a new namespace, but it is performed **after** the utility and entry‑point classes have been migrated, preventing premature reference breakage. | [Data: Reports (0, 13, 10, 11, +more)] |
| **3. Static utility class `TrainCatalog`** | Refactor its private constructor and static `getTrains()` method (e.g., introduce a builder or dependency‑injection). | `TrainCatalog` is **isolated** – it is only referenced by `TrainApp`. Changing it now will not ripple through other components. | [Data: Reports (5, 27, 40, 22, +more)] |
| **4. Entry‑point class `TrainApp`** | Move `TrainApp` to the new package (or adjust its visibility) after `TrainCatalog` is stable. `TrainApp` merely calls `TrainCatalog.getTrains()` and prints the result. | Because its only dependency is the now‑stable `TrainCatalog`, migrating `TrainApp` will not disturb other parts of the system. | [Data: Reports (3, 31, 32, 30, +more)] |
| **5. Core data model `Train`** | Refactor immutable fields, add `Objects.requireNonNull` checks, and improve `toString` formatting. Perform this **last** because `Train` is the central model used by `TrainCatalog`, `TrainApp`, and many accessor methods. | Delaying the core class avoids **cascading breakages**; once the surrounding infrastructure is stable, the impact of changes to `Train` is contained and easier to test. | [Data: Reports (4, 17, 19, 21, +more)] |
| **6. Date‑time formatting (`OFPATTERN`)** | Update the `DateTimeFormatter.ofPattern` usage after the `Train` class has been refactored. | Changing the formatter **after** the core model is stable ensures the new pattern does not propagate prematurely and cause unexpected parsing/formatting errors. | [Data: Reports (20, +more)] |

### How the Order Reduces Breaking Changes  

1. **Isolation First** – By starting with build files and then the package namespace, we touch artefacts that have no direct runtime links to the business logic.  
2. **Utility Before Core** – `TrainCatalog` and `TrainApp` sit on the periphery of the system; they can be refactored with minimal downstream impact.  
3. **Core Last** – The `Train` class is the hub of the domain model. Refactoring it after all callers are stable confines any incompatibilities to a single, well‑tested migration window.  
4. **Formatting After Core** – Date‑time format changes are purely cosmetic/utility concerns; applying them after the core model is settled prevents premature propagation of format‑related bugs.

Following this ordered approach should allow the codebase to evolve with **few regressions**, clear verification checkpoints after each step, and a predictable path toward the new package structure and modernised implementation.

### Known Vulnerabilities

## Security‑related assessment  

### Runtime stack versions present in the project  

| Component | Version (as declared in the source) | Evidence |
|-----------|--------------------------------------|----------|
| **Java language / JDK** | 1.8 (source‑ and target level) | `pom.xml` lists `<maven.compiler.source>1.8</maven.compiler.source>` and `<maven.compiler.target>1.8</maven.compiler.target>`【Sources (0)】 |
| **Maven Compiler Plugin** | 3.13.0 | Declared in the `<plugin>` section of `pom.xml`【Sources (0)】; also referenced in the *northpolesouthern‑train* report【Reports (2)】 |
| **Maven JAR Plugin** | 3.4.2 | Declared in the `<plugin>` section of `pom.xml`【Sources (0)】; also referenced in the *northpolesouthern‑train* report【Reports (2)】 |
| **Standard‑library packages** (java.time, java.util, java.lang.System) | Part of the JDK 8 runtime | Mentioned in the code and the *COM.NORTHPOLESOUTHERN.TRAIN* report【Reports (4)】; Entity records for `JAVA` (id 11), `MAVEN‑COMPILER‑PLUGIN` (id 4) and `MAVEN‑JAR‑PLUGIN` (id 5) list them as the only dependencies. |

No additional frameworks, databases, or middleware are used.

### Known CVE / End‑of‑Life (EOL) status  

| Component | Current security posture (as of 2024‑06) | Comments |
|-----------|------------------------------------------|----------|
| **Java 8 (JDK 1.8)** | Actively maintained under Oracle’s **Long‑Term Support (LTS)** program, but only the most recent public update receives security patches. Older update releases (e.g., 8u31, 8u45) are **EOL** and may contain unpatched CVEs. The project does not specify the exact update level, so a definitive CVE check cannot be performed. | If the deployment runs a recent JDK 8 update (e.g., 8u361 or later), known critical CVEs such as CVE‑2021‑44228 (Log4j) do **not** apply because Log4j is not used. |
| **Maven Compiler Plugin 3.13.0** | No publicly disclosed CVEs for this version (the plugin is a thin wrapper around the Java compiler). It is a recent release (2023‑2024) and not marked EOL. | The plugin itself does not introduce runtime code; it only compiles source. |
| **Maven JAR Plugin 3.4.2** | No known CVEs for this version. It is also a recent release and actively maintained. | The plugin only packages compiled classes into a JAR and sets the manifest; it does not affect runtime security. |
| **Standard‑library classes** (java.time, java.util, java.lang.System) | Covered by the JDK 8 security updates. No additional vulnerabilities beyond those that might exist in the underlying JDK. | The code uses only immutable data holders and safe APIs (e.g., `Objects.requireNonNull`, `System.lineSeparator`). |

### Code‑level observations  

* The `Train` class is **immutable** and validates all constructor arguments with `Objects.requireNonNull`, eliminating `null`‑related crashes.  
* No external libraries (e.g., logging frameworks, XML parsers, network stacks) are imported, reducing the attack surface.  
* The only I/O performed is `System.out.println` in `TrainApp.main`, which writes to standard output and does not process untrusted input.  

All of these points are documented in the *COM.NORTHPOLESOUTHERN.TRAIN* report【Reports (4)】 and the source files themselves【Sources (4, 7)】.

## Verdict  

- **No direct security vulnerabilities** are evident in the application code or its declared dependencies.  
- The **runtime stack** consists solely of Java 8 and two Maven plugins, all of which are either still supported (Java 8 LTS) or have no known CVEs for the listed versions.  
- **Caveat:** The exact JDK 8 update level is not specified; using an up‑to‑date JDK 8 release is recommended to ensure all critical CVEs are patched.  

**Recommendation:** Deploy the application on a recent Java 8 update (or consider upgrading to a newer LTS release such as Java 11 or Java 17) and keep Maven plugins at their current versions. No further remediation is required based on the available data.

### Known Dependencies Requiring Upgrade

## Dependency‑Upgrade Overview  

| Component | Current version (as recorded) | Recommended target version* | End‑of‑life status |
|-----------|------------------------------|-----------------------------|--------------------|
| **Java runtime** | 1.8 (source/target set in `pom.xml`) | 21 (latest LTS) | **EOL** – public updates for Oracle JDK 8 ended in 2019; many libraries have already dropped support for 1.8. |
| **Maven Compiler Plugin** | 3.13.0 (declared in the `pom.xml`) | 3.13.0 or newer (check Maven Central) | Not EOL, but newer patch releases may contain bug‑fixes and support for newer Java releases. |
| **Maven JAR Plugin** | 3.4.2 (declared in the `pom.xml`) | 3.4.2 or newer (check Maven Central) | Not EOL, but newer releases add improvements and compatibility fixes. |
| **Maven build tool** | Unspecified in the data (only referenced as a build system) | 3.9.x (latest stable) | Maven 3.6+ is still supported; older 3.0‑3.5 series are effectively superseded. |

\*The “recommended target version” reflects the most recent stable release that is widely adopted in the Java ecosystem (e.g., JDK 21 is the current LTS as of 2024).  

### Sources  

- Java version is set to `1.8` in the `pom.xml` properties and referenced in the **Entities** table entry for `JAVA` (id 11) and the **Sources** record for the `pom.xml` (id 0) [Data: Entities (11); Sources (0)].  
- Maven Compiler Plugin version **3.13.0** is described in the **Entities** table (id 4) [Data: Entities (4)].  
- Maven JAR Plugin version **3.4.2** is described in the **Entities** table (id 5) [Data: Entities (5)].  
- The `pom.xml` (source id 1) shows the plugin declarations and the Java source/target settings [Data: Sources (1)].  
- The relationship that the `pom.xml` drives the build with Maven is captured in the **Relationships** table (id 13) [Data: Relationships (13)].  

## Recommended Upgrade Order  

1. **Upgrade the Java runtime first**  
   *Reason*: All compiled code (including the Maven plugins) runs on the JDK. Moving to JDK 21 removes the EOL status, provides language features (e.g., records, sealed classes) and performance improvements, and is a prerequisite for many newer plugin releases.  

2. **Update Maven itself (if an older version is in use)**  
   *Reason*: A recent Maven version (≥ 3.9) better understands the newer JDK toolchains and can resolve the latest plugin releases without compatibility warnings.  

3. **Upgrade Maven Compiler Plugin**  
   *Reason*: Newer plugin releases add explicit support for recent JDKs (e.g., `release` flag for JDK 21) and may include bug fixes.  

4. **Upgrade Maven JAR Plugin**  
   *Reason*: While the current 3.4.2 release works with newer JDKs, newer versions may improve manifest handling and reproducible builds.  

### Practical Steps  

| Step | Action | Command / Change |
|------|--------|-------------------|
| 1 | Install JDK 21 and set `JAVA_HOME` accordingly. | `sdk install java 21.0.2-open` (or use your preferred installer). |
| 2 | Verify Maven version; upgrade if < 3.9. | `mvn -v` → download latest from https://maven.apache.org/download.cgi. |
| 3 | Update `pom.xml` properties to target the new JDK. | ```xml<br><properties><br>    <maven.compiler.source>21</maven.compiler.source><br>    <maven.compiler.target>21</maven.compiler.target><br></properties>``` |
| 4 | Change plugin versions (if newer releases exist). | ```xml<br><plugin><groupId>org.apache.maven.plugins</groupId><br>    <artifactId>maven-compiler-plugin</artifactId><br>    <version>3.13.0</version> <!-- replace with newer if available --> <br></plugin>``` |
| 5 | Run a clean build and address any compilation warnings. | `mvn clean verify` |

## Why No Other Stack Components Appear  

The supplied data set only describes a pure Java library built with Maven; there are **no** references to web frameworks (e.g., Spring, Angular), databases, or middleware. Consequently, the only upgrade considerations are the Java runtime and the Maven‑related build plugins. If additional components (e.g., a database driver) are added later, a similar review should be performed for those dependencies.  

### Detailed Migration Plan

## Migration Plan

| # | Component | Git Repo | Description | Relevant Files | Complexity | Complexity Score | Complexity Justification | Rank Justification |
|---|-----------|----------|-------------|----------------|------------|------------------|--------------------------|--------------------|
| 1 | **Train** |  | Core domain class that models a train; uses only standard‑library types (e.g., `java.time`, `java.util`). It has **no internal project dependencies** and is referenced by other components such as `TrainCatalog` and `TrainApp` [Data: Reports (4, 17, 21, 0, 6, +more)]. |  | low | 2 | The class is self‑contained, touches only the JDK, and does not require any runtime version change, so the migration effort is minimal. | As the leaf node with the fewest dependencies, it should be migrated first to provide a stable foundation for downstream components. |
| 2 | **TrainCatalog** |  | Provides an immutable list of `Train` objects via a static `getTrains()` method. It **depends on the `Train` class** but has no other project‑level dependents [Data: Reports (5, 23, 24, 16, 2, +more)]. |  | medium | 4 | Requires the `Train` class to be present, adding a modest amount of integration work, but still only uses standard‑library types. | After `Train` is migrated, `TrainCatalog` can be updated safely; its single dependency makes it the next logical step. |
| 3 | **TrainApp** |  | Application entry point with a private constructor and static `main` method; calls `TrainCatalog.getTrains()` to drive the program [Data: Reports (3, 2, 15, 11, 1, +more)]. |  | medium | 5 | Depends on `TrainCatalog` (and transitively on `Train`). No additional external libraries, but being the executable component adds a slight increase in risk. | Once the catalog is stable, migrating the entry point ensures the whole runtime flow is consistent with the new codebase. |
| 4 | **Maven build configuration** (pom.xml & plugins) |  | Defines the build lifecycle, packaging, and `mainClass`. It does **not affect runtime code directly**, making it a low‑risk change, though it must be aligned with the migrated classes [Data: Reports (14, 13, 10, 9, +more)]. |  | low | 3 | No code changes, only build‑tool configuration; the only complexity is ensuring plugin versions remain compatible with the target runtime. | Updating the build after the core classes are migrated avoids unnecessary rebuild failures and keeps the build process in sync. |
| 5 | **Repository metadata** (top‑level project structure) |  | Top‑level artifact that aggregates all modules and has the most dependents. It should be migrated last to avoid breaking downstream components [Data: Reports (3, 13, +more)]. |  | high | 7 | Changing the overall project structure can impact many downstream consumers; careful coordination is required, especially if the structure influences CI/CD pipelines. | As the component with the greatest number of dependents, it is safest to migrate it after all internal code and build configuration have been stabilized. |

### How the Plan Was Derived

* **Leaf‑first ordering** – The ranking follows a dependency‑aware sequence: start with the most isolated class (`Train`), then the class that depends on it (`TrainCatalog`), followed by the application entry point (`TrainApp`). Build configuration and repository metadata are placed after the code because they orchestrate the compiled artifacts rather than contain business logic.
* **Complexity scoring** – Scores combine two factors: (1) **dependency depth** (more dependents → higher risk) and (2) **runtime version impact**. No major runtime version upgrades were mentioned in the source data, so the scores reflect only structural complexity.
* **Evidence preservation** – All statements are backed by the original data references; no additional assumptions (e.g., repository URLs or file paths) were introduced.

### Integration Points

## Overview  

The available evidence shows that the codebase does **not** connect to any external databases, REST/SOAP APIs, message‑queues, or third‑party infrastructure services. All data is defined in‑process (e.g., in `TrainCatalog`) and accessed through ordinary method calls. The only external dependencies are the **Java runtime** that executes the application and the **Maven build infrastructure** that compiles and packages the code.  

---

## Integration Points  

| External system | Component that connects to it | Integration type | Nature of the interaction | Evidence |
|-----------------|------------------------------|------------------|---------------------------|----------|
| **Java Runtime (JDK / JVM)** | All application classes (`TrainApp`, `TrainCatalog`, `Train`, utility classes) | Infrastructure service – execution environment | The JVM provides core services such as console I/O, collection handling, and the `java.time`, `java.util`, `java.lang` libraries that the code relies on at runtime. | [Data: Reports (3, 13, 2, 4, 5)] |
| **Maven build system** | `pom.xml` and Maven plugins (`maven‑compiler‑plugin`, `maven‑jar‑plugin`) | Build/infrastructure service | Maven orchestrates the compile‑test‑package lifecycle, resolves dependencies, and writes the `Main-Class` (`TrainApp`) into the JAR manifest, thereby integrating the source code with the Maven service. | [Data: Reports (14, 10, 2, 3, 13)] |

---

## No External Service Integrations  

The dataset explicitly states that there are **no** references to external databases, APIs, message queues, or other third‑party services. All domain data lives inside the `TrainCatalog` class and is accessed directly through in‑process calls, confirming the absence of integration points beyond the runtime and build layers.  

| Supposed integration type | Confirmation of absence | Evidence |
|--------------------------|--------------------------|----------|
| Database | No database connections or drivers are referenced. | [Data: Reports (0, 1, 6, 7, 8)] |
| External API | No HTTP client libraries or endpoint URLs are present. | [Data: Reports (0, 1, 6, 7, 8)] |
| Message queue | No messaging frameworks (e.g., JMS, Kafka) appear in the code. | [Data: Reports (0, 1, 6, 7, 8)] |
| Third‑party infrastructure | No cloud services, authentication providers, or external storage are mentioned. | [Data: Reports (0, 1, 6, 7, 8)] |

---

## Implications  

1. **Deployment simplicity** – Since the application does not depend on external services, it can be packaged and run on any JVM‑compatible host without provisioning databases or networked APIs.  

2. **Testing ease** – Unit tests can exercise the full functionality without mocks or stubs for external systems; the only prerequisite is a compatible JDK.  

3. **Scalability limits** – The static, in‑process data model (`TrainCatalog`) may become a bottleneck if the domain grows, because there is no external persistence layer to off‑load state.  

4. **Build reproducibility** – Maven governs the entire build pipeline, ensuring consistent compilation and packaging across environments.  

---

### Bottom line  

The codebase integrates solely with the Java runtime (providing the execution platform) and the Maven build infrastructure (handling compilation and packaging). No other external systems such as databases, APIs, or messaging services are involved.

### Regression Risk Assessment

## High‑Risk Areas During Migration  

Below is a consolidated view of the code sections that are most vulnerable when the project is moved to a new build system, Java version, or runtime environment. For each area the likely failure mode, its root cause, and the validation steps that **shall** be performed before and after migration are outlined.

---

### 1. `TrainCatalog.getTrains()` – Immutable List Construction  

**What may break**  
* The method builds the catalog with `Arrays.asList` followed by `Collections.unmodifiableList`.  
* Any change to the private constructor or to the list‑creation logic (e.g., switching to a mutable `ArrayList`) can violate the immutability contract, allowing callers to modify the list at runtime.  
* An altered conversion may also introduce `NullPointerException`s if a `null` element is introduced during the array‑to‑list step.

**Why it is risky**  
* The rest of the application (including `TrainApp`) relies on the list being read‑only.  
* Breaking immutability can cause subtle state‑corruption bugs that are hard to trace.

**Validation checklist**  

| Phase | Checks |
|-------|--------|
| **Before migration** | • Call `TrainCatalog.getTrains()` and verify the returned object is an instance of `java.util.List` that is **unmodifiable** (attempting `add`/`remove` must throw `UnsupportedOperationException`).<br>• Confirm the list contains the expected `Train` objects and no `null` entries. |
| **After migration** | • Repeat the unmodifiable‑list test to ensure the contract still holds.<br>• Run a regression test that iterates over the list and prints the catalog; the output must match the pre‑migration baseline. |

[Data: Reports (5, 23, 9, 24, 10, +more)]

---

### 2. Maven Build Configuration (`pom.xml`)  

**What may break**  
* The `pom.xml` pins the **compiler‑plugin** to Java 1.8, defines the `jar‑plugin` manifest, and specifies the `mainClass`.  
* Migrating to a different build tool or upgrading the Java version can cause compilation failures, missing classes in the JAR, or an incorrect manifest entry, resulting in a JAR that will not launch.

**Why it is risky**  
* The whole delivery pipeline (compile → package → run) hinges on these settings.  
* A mis‑configured manifest or incompatible bytecode version will surface only at runtime, often as a “Could not find or load main class” error.

**Validation checklist**  

| Phase | Checks |
|-------|--------|
| **Before migration** | • Execute `mvn clean package` and confirm a successful build.<br>• Inspect the generated JAR to ensure it contains all expected `.class` files.<br>• Run the JAR (`java -jar target/…jar`) and verify that `TrainApp.main()` executes and prints the catalog. |
| **After migration** | • Re‑run the full build with the new system and confirm no compilation errors.<br>• Open the new artifact and check that the manifest still points to the correct `mainClass`.<br>• Execute the artifact and compare its console output with the baseline. |

[Data: Reports (14, 13, 10, 2, 11, +more)]

---

### 3. `Train` Constructor Null‑Checking  

**What may break**  
* The constructor uses `java.util.Objects.requireNonNull` for each argument.  
* If the migration changes the validation approach (e.g., replaces it with custom checks) or targets a Java version where `requireNonNull` is unavailable, the constructor could accept `null` values.

**Why it is risky**  
* Allowing `null` fields can lead to `NullPointerException`s later in the code path (e.g., when `toString` or schedule calculations are performed).  
* The contract that a `Train` instance is always fully populated would be broken, undermining data integrity.

**Validation checklist**  

| Phase | Checks |
|-------|--------|
| **Before migration** | • Attempt to instantiate `new Train(null, …)` for each parameter and verify that a `NullPointerException` is thrown immediately.<br>• Create a valid `Train` and assert that all getters return the supplied non‑null values. |
| **After migration** | • Repeat the null‑argument tests to ensure the same exceptions are raised.<br>• Run a suite that creates a fully populated `Train` and checks that its state matches the pre‑migration instance. |

[Data: Reports (19, 4, 17, 21, 0, +more)]

---

### 4. Tight Coupling Between `TrainApp` and `TrainCatalog`  

**What may break**  
* `TrainApp.main()` directly calls `TrainCatalog.getTrains()`.  
* Any change to the static method’s signature, return type, or package location will cause a compile‑time error, preventing the application from starting.

**Why it is risky**  
* The coupling eliminates an abstraction layer; refactoring the catalog API without updating `TrainApp` will break the build.  
* This is a classic “single point of failure” that can be triggered by seemingly innocuous changes (e.g., moving `TrainCatalog` to a different package).

**Validation checklist**  

| Phase | Checks |
|-------|--------|
| **Before migration** | • Compile the whole project and ensure `TrainApp` compiles without warnings.<br>• Run `TrainApp` and verify that the printed catalog matches the expected format. |
| **After migration** | • Re‑compile the project; any missing method or type errors must be resolved.<br>• Execute `TrainApp` again and compare its output line‑by‑line with the baseline. |

[Data: Reports (2, 3, 15, 11, 12, +more)]

---

### 5. `Train.toString()` Formatting  

**What may break**  
* The method builds a multi‑line schedule string using `DateTimeFormatter.ofPattern` and `System.lineSeparator()`.  
* Modifying the pattern, the formatter usage, or the line‑separator call can change the output format or raise `IllegalArgumentException` (e.g., if an invalid pattern is supplied).

**Why it is risky**  
* Many downstream processes (logs, UI displays, test assertions) depend on the exact string layout, including line breaks.  
* A subtle change in formatting can cause test failures or mis‑interpretation of schedule data.

**Validation checklist**  

| Phase | Checks |
|-------|--------|
| **Before migration** | • Create a representative `Train` instance and capture the exact string returned by `toString()`.  
• Verify that the string contains the expected pattern and line separators. |
| **After migration** | • Generate the same string for an equivalent `Train` and perform a strict equality comparison with the pre‑migration result.  
• Ensure that no `IllegalArgumentException` is thrown during formatting. |

[Data: Reports (4, 1, 18, 20, 21, +more)]

---

## Summary  

The migration effort should prioritize **five key risk zones**:

1. **Immutable catalog list** – guard against accidental mutability.  
2. **Build configuration** – keep the Maven (or new build) pipeline producing a runnable JAR.  
3. **Constructor null checks** – preserve strict non‑null contracts.  
4. **Direct static coupling** – maintain method signatures and package locations.  
5. **String formatting** – retain the exact output layout for `Train.toString()`.

For each zone, the outlined **pre‑ and post‑migration validation steps** will provide a safety net that detects regressions early, ensuring functional parity after the migration.

### Characterization Tests Generation Plan

# Characterization‑Test Generation Plan  
**Project:** *North Pole Southern Train* (Java Maven)  
**Goal:** Produce a comprehensive, automated test suite that captures the existing behaviour of the legacy code base and serves as a regression‑safety net while the application is refactored for RHEL 10 compatibility.  

---

## 1. Objectives  

| # | Objective | Success Metric |
|---|-----------|----------------|
| 1 | Capture **functional** behaviour of all public APIs (`TrainCatalog.getTrains()`, `TrainApp.main()`) and any side‑effects (console output). | ≥ 90 % statement/branch coverage. |
| 2 | Detect **security‑relevant** regressions (e.g., unsafe handling of `null`, malformed data, format strings). | No new findings in OWASP Dependency‑Check / SpotBugs after baseline. |
| 3 | Provide a **mutation‑testing** baseline to prove test quality. | Mutation score ≥ 80 % (or as high as feasible given the simple code). |
| 4 | Integrate tests into the **CI pipeline** and generate artefacts (coverage reports, mutation reports) for every build on RHEL 10. | CI passes on every commit; reports published to the internal dashboard. |
| 5 | Enable **future refactoring** (e.g., modularisation, externalisation of data) without breaking existing behaviour. | All baseline tests pass after any refactor. |

---

## 2. Scope  

| Component | Reason for Inclusion | Exclusions |
|-----------|----------------------|------------|
| `com.northpolesouthern.train.Train` (data class) | Core domain model – getters, `equals`, `hashCode`, `toString`. | None – fully covered. |
| `com.northpolesouthern.train.TrainCatalog` | Static factory, immutable list, data integrity. | Internal private constructor (no direct test needed). |
| `com.northpolesouthern.train.TrainApp` | Entry point – prints catalog; validates that the console output matches the expected format. | External environment (e.g., OS‑level console encoding) – will be normalised in test harness. |
| Build artefacts (`pom.xml`, Maven plugins) | Verify that the compiled JAR matches expectations (manifest, version). | Third‑party libraries – none are used. |

---

## 3. Test‑Strategy Overview  

| Layer | Technique | Tools | Deliverable |
|-------|-----------|-------|-------------|
| **Unit** | Characterization tests for each public method; use *parameterised* tests where possible to exercise all enum/constant values. | JUnit 5, AssertJ, Mockito (only for static‑method stubbing if needed). | `src/test/java` with 100 % method coverage. |
| **Integration** | Execute `TrainApp.main()` in a sandboxed JVM, capture `System.out`, compare to a *golden* snapshot. | JUnit 5 + System‑out capture (e.g., `SystemLambda` or `System Rules`). | One integration test that validates end‑to‑end output. |
| **Static‑analysis** | Detect hidden bugs, security smells, and enforce coding standards. | SpotBugs, PMD, Checkstyle, OWASP Dependency‑Check (even though no external deps). | CI quality gate. |
| **Mutation Testing** | Quantify the effectiveness of the test suite. | PIT (Pitest) Maven plugin. | Mutation report with score. |
| **Code‑Coverage** | Measure statement/branch coverage to guide test completeness. | JaCoCo Maven plugin. | HTML coverage report. |
| **Cross‑Platform Validation** | Run the full suite on RHEL 10 containers to guarantee platform compatibility. | Docker (RHEL 10 base image) + Maven wrapper. | CI job “RHEL‑10‑validation”. |

---

## 4. Detailed Test‑Creation Process  

1. **Environment Bootstrap**  
   - Create a **Dockerfile** based on `registry.access.redhat.com/ubi8/ubi` (RHEL 8) and upgrade to RHEL 10 repositories (or use the official RHEL 10 container if available).  
   - Install JDK 1.8, Maven 3.8+, and required test tools (`git`, `curl`).  
   - Verify `java -version` and `mvn -v` match the legacy build.

2. **Baseline Behaviour Capture**  
   - Run the current `TrainApp` on the RHEL 10 image and capture its console output to a **golden file** (`target/golden/TrainApp.output.txt`).  
   - Store the golden file in version control (e.g., `src/test/resources/golden/TrainApp.output.txt`).  

3. **Unit Test Skeleton Generation**  
   - Use **IntelliJ IDEA** or **Eclipse** to auto‑generate test classes for each public class (`TrainTest`, `TrainCatalogTest`).  
   - For each public method, create a *characterization* test that:  
     - Calls the method with **the exact inputs observed in production** (derived from the golden output and any existing logs).  
     - Asserts that the **observable state** (return value, object equality, collection size, ordering) matches the captured baseline.  
   - Where the method is pure (e.g., getters), assert the exact values; where it returns a collection, assert **immutability** (attempt modification and expect `UnsupportedOperationException`).  

4. **Console‑Output Test**  
   - Write a single integration test that:  
     - Invokes `TrainApp.main(new String[0])` inside a captured `System.out` context.  
     - Normalises line endings (`\r\n` → `\n`) and trims trailing whitespace.  
     - Compares the result to the golden file using `assertThat(actual).isEqualToNormalizingNewlines(expected)`.  

5. **Edge‑Case Exploration (Security‑Focused)**  
   - Although the code has no external inputs, verify defensive behaviour:  
     - Pass `null` to `TrainCatalog.getTrains()` (if method signature permits) and assert that a `NullPointerException` is thrown (or that the method is *null‑safe*).  
     - Attempt to mutate the returned list and confirm immutability.  
   - These tests are **characterization** – they codify the current (possibly unsafe) behaviour, which will later be hardened.  

6. **Coverage Instrumentation**  
   - Add the **JaCoCo Maven plugin** to `pom.xml` with `prepare-agent` in the `initialize` phase and `report` in the `verify` phase.  
   - Enforce a **minimum coverage rule** (e.g., `line>90`) in the `jacoco:check` goal.  

7. **Mutation‑Testing Setup**  
   - Add the **Pitest Maven plugin**:  
     ```xml
     <plugin>
       <groupId>org.pitest</groupId>
       <artifactId>pitest-maven</artifactId>
       <version>1.14.0</version>
       <configuration>
         <targetClasses>com.northpolesouthern.train.*</targetClasses>
         <targetTests>com.northpolesouthern.train.*Test</targetTests>
         <mutationThreshold>80</mutationThreshold>
         <coverageThreshold>90</coverageThreshold>
       </configuration>
     </plugin>
     ```  
   - Run `mvn org.pitest:pitest-maven:mutationCoverage` on the RHEL 10 image; capture the HTML report.  

8. **CI Integration**  
   - **GitHub Actions / GitLab CI** (or internal Jenkins) pipeline steps:  
     1. `docker build -t train-app-test .` (RHEL 10 image).  
     2. `docker run --rm train-app-test mvn clean verify` – executes unit tests, JaCoCo, and Pitest.  
     3. Publish `target/site/jacoco` and `target/pit-reports` as artefacts.  
     4. Fail the build if coverage or mutation thresholds are not met.  

9. **Documentation & Baseline Artefacts**  
   - Add a `README.md` under `src/test/java/com/northpolesouthern/train` describing:  
     - How the golden file was generated.  
     - How to update the baseline (run `TrainApp` on a clean RHEL 10 image, replace the golden file, and re‑run tests).  
   - Store the **baseline mutation score** and **coverage percentages** in a `docs/baseline-metrics.md` file for future comparison.  

---

## 5. Security‑Focused Characterization  

Even though the application has no external inputs, the following security‑related aspects must be captured:

| Concern | Test Idea (characterization) | Why it matters |
|---------|------------------------------|----------------|
| **Immutable Collections** | Verify that the list returned by `TrainCatalog.getTrains()` throws `UnsupportedOperationException` on `add/remove`. | Prevents accidental mutation that could be exploited if the list were exposed to untrusted code. |
| **String Formatting** | Ensure that `Train.toString()` does not inadvertently expose internal state that could be used for injection (e.g., unescaped `%` in `String.format`). | Guard against future changes that might introduce format‑string vulnerabilities. |
| **Null Handling** | Call public methods with `null` arguments (if any) and assert the current exception type/message. | Documents the present defensive posture; later we can decide to tighten or relax it. |
| **Logging / Console Output** | Capture the exact console output format (including timestamps) and assert it matches the baseline. | Guarantees that no accidental information leakage (e.g., full timestamps) is introduced. |

All of the above are **characterization** – they lock in the *existing* security posture, providing a reference point for any hardening work.

---

## 6. Risk Assessment & Mitigations  

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Static methods & final classes hinder mocking** | May limit ability to isolate behaviour. | Use **PowerMock** (if needed) only for legacy static calls; otherwise rely on direct calls because the code is self‑contained. |
| **Golden file drift** | Future legitimate changes could cause false failures. | Document the **golden‑file update process** and require a peer‑reviewed PR when the output changes. |
| **Mutation testing on tiny code base yields low mutation count** | Score may be artificially high/low. | Complement mutation score with **manual review** of surviving mutants; treat the score as a *trend* rather than an absolute metric. |
| **RHEL 10 container differences** (e.g., default locale) affect output | Test failures unrelated to code. | Set `LANG=C.UTF-8` and `LC_ALL=C.UTF-8` in the Dockerfile to normalise locale. |
| **Future refactor introduces external dependencies** | Current test suite may not cover new integration points. | Plan to extend the suite incrementally; the baseline remains a stable anchor. |

---

## 7. Timeline (Suggested Sprint)  

| Day | Activity |
|-----|----------|
| 1‑2 | Set up RHEL 10 Docker image, verify build environment. |
| 3‑4 | Capture golden console output; commit to repo. |
| 5‑7 | Generate unit‑test skeletons, write characterization assertions for `Train` and `TrainCatalog`. |
| 8‑9 | Implement the `TrainApp` integration test (console capture). |
| 10 | Add JaCoCo configuration, run coverage, adjust tests to reach ≥ 90 % coverage. |
| 11‑12 | Add Pitest configuration, run mutation analysis, address surviving mutants (if any). |
| 13 | Integrate tests into CI pipeline, verify artefact publishing. |
| 14 | Documentation, final review, hand‑off to refactoring team. |

---

## 8. Deliverables  

| Artefact | Location | Description |
|----------|----------|-------------|
| **Test source** | `src/test/java/com/northpolesouthern/train/` | JUnit 5 characterization tests for all public APIs. |
| **Golden output** | `src/test/resources/golden/TrainApp.output.txt` | Baseline console output for `TrainApp`. |
| **JaCoCo report** | `target/site/jacoco/index.html` | Statement/branch coverage details. |
| **Pitest mutation report** | `target/pit-reports/*` | Mutant survival analysis. |
| **CI pipeline definition** | `.github/workflows/ci.yml` (or Jenkinsfile) | Automated build, test, coverage, mutation steps on RHEL 10. |
| **Documentation** | `docs/characterization-plan.md` & `docs/baseline-metrics.md` | Test strategy, update procedures, baseline metrics. |
| **Dockerfile** | `docker/Dockerfile` | RHEL 10 build & test environment. |

---

## 9. Acceptance Criteria  

- All **public methods** are exercised and verified against the captured baseline.  
- **JaCoCo** reports ≥ 90 % line coverage and ≥ 80 % branch coverage.  
- **Pitest** mutation score ≥ 80 % (or the highest achievable given the code size).  
- CI pipeline runs on a **RHEL 10** container and passes on the *current* code base without manual intervention.  
- Documentation is complete and reviewed by the security and refactoring leads.  

---

### Next Steps  

1. **Kick‑off meeting** with the legacy‑code owners to confirm the golden output and any known quirks.  
2. **Provision** a RHEL 10 build node (or container registry) for the team.  
3. **Create** the initial Git branch (`characterization-baseline`) and push the scaffolded test suite.  

Once the baseline is green, the team can proceed with refactoring (e.g., modularising the catalog, adding external data sources) while the characterization suite guarantees functional and security parity.

### LLM Token Usage & Cost Summary

```
                      LLM TOKEN USAGE & COST SUMMARY
==============================================================================
 Total LLM Invocations : 15
 Total Prompt Tokens   : 15,892
 Total Output Tokens   : 14,625
 Total Tokens Used     : 30,517
 Estimated Total Cost  : $0.1479
------------------------------------------------------------------------------
 Source / Model                   Calls   Prompt     Output     Total      Est. Cost 
------------------------------------------------------------------------------
 e5-mistral-7b-instruct           3       477        0          477        $0.0001   
 GraphRAG Local Search (openai... 3       4,146      2,377      6,523      $0.0273   
 GraphRAG Global Search (opena... 5       1,964      4,625      6,589      $0.0409   
 GraphRAG Chat (openai/gpt-oss... 4       9,305      7,623      16,928     $0.0796   
```

### Code Migration Plan (JSON)

```json
{
  "code_migration_plan": [
    {
      "migration_order": 1,
      "component": "Train",
      "git_repo": "",
      "description": "Core domain class that models a train; uses only standard‑library types (e.g., `java.time`, `java.util`). It has **no internal project dependencies** and is referenced by other components such as `TrainCatalog` and `TrainApp` [Data: Reports (4, 17, 21, 0, 6, +more)].",
      "relevant_files": [],
      "complexity": "low",
      "complexity_score": 2,
      "complexity_justification": "The class is self‑contained, touches only the JDK, and does not require any runtime version change, so the migration effort is minimal.",
      "rank_justification": "As the leaf node with the fewest dependencies, it should be migrated first to provide a stable foundation for downstream components."
    },
    {
      "migration_order": 2,
      "component": "TrainCatalog",
      "git_repo": "",
      "description": "Provides an immutable list of `Train` objects via a static `getTrains()` method. It **depends on the `Train` class** but has no other project‑level dependents [Data: Reports (5, 23, 24, 16, 2, +more)].",
      "relevant_files": [],
      "complexity": "medium",
      "complexity_score": 4,
      "complexity_justification": "Requires the `Train` class to be present, adding a modest amount of integration work, but still only uses standard‑library types.",
      "rank_justification": "After `Train` is migrated, `TrainCatalog` can be updated safely; its single dependency makes it the next logical step."
    },
    {
      "migration_order": 3,
      "component": "TrainApp",
      "git_repo": "",
      "description": "Application entry point with a private constructor and static `main` method; calls `TrainCatalog.getTrains()` to drive the program [Data: Reports (3, 2, 15, 11, 1, +more)].",
      "relevant_files": [],
      "complexity": "medium",
      "complexity_score": 5,
      "complexity_justification": "Depends on `TrainCatalog` (and transitively on `Train`). No additional external libraries, but being the executable component adds a slight increase in risk.",
      "rank_justification": "Once the catalog is stable, migrating the entry point ensures the whole runtime flow is consistent with the new codebase."
    },
    {
      "migration_order": 4,
      "component": "Maven build configuration (pom.xml & plugins)",
      "git_repo": "",
      "description": "Defines the build lifecycle, packaging, and `mainClass`. It does **not affect runtime code directly**, making it a low‑risk change, though it must be aligned with the migrated classes [Data: Reports (14, 13, 10, 9, +more)].",
      "relevant_files": [],
      "complexity": "low",
      "complexity_score": 3,
      "complexity_justification": "No code changes, only build‑tool configuration; the only complexity is ensuring plugin versions remain compatible with the target runtime.",
      "rank_justification": "Updating the build after the core classes are migrated avoids unnecessary rebuild failures and keeps the build process in sync."
    },
    {
      "migration_order": 5,
      "component": "Repository metadata (top‑level project structure)",
      "git_repo": "",
      "description": "Top‑level artifact that aggregates all modules and has the most dependents. It should be migrated last to avoid breaking downstream components [Data: Reports (3, 13, +more)].",
      "relevant_files": [],
      "complexity": "high",
      "complexity_score": 7,
      "complexity_justification": "Changing the overall project structure can impact many downstream consumers; careful coordination is required, especially if the structure influences CI/CD pipelines.",
      "rank_justification": "As the component with the greatest number of dependents, it is safest to migrate it after all internal code and build configuration have been stabilized."
    }
  ]
}
```

### RHEL-Enhanced Code Migration Plan (JSON)

```json
{
  "rhel_enhanced_code_migration_plan": [
    {
      "migration_order": 1,
      "component": "Train",
      "git_repo": "",
      "description": "Core domain class that models a train; uses only standard‑library types (e.g., java.time, java.util). It has no internal project dependencies and is referenced by other components such as TrainCatalog and TrainApp.",
      "relevant_files": [],
      "complexity": "low",
      "complexity_score": 2,
      "complexity_justification": "The class is self‑contained, touches only the JDK, and does not require any runtime version change, so the migration effort is minimal.",
      "rank_justification": "As the leaf node with the fewest dependencies, it should be migrated first to provide a stable foundation for downstream components.",
      "rhel10_compatibility_issues": "No OS‑specific dependencies, but ensure the code does not rely on SHA‑1 or other deprecated cryptographic algorithms, as RHEL 10 enforces stricter cryptographic policies. Verify compatibility with the target JDK version.",
      "rhel10_compatibility_issues_reference_sources": [
        "BREAKING CHANGES AND INCOMPATIBILITIES – Cryptography (SHA-1)",
        "RUNTIME AND MIDDLEWARE CONSIDERATIONS – Java"
      ],
      "runtime_upgrade_required": "JDK 8 → JDK 21"
    },
    {
      "migration_order": 2,
      "component": "TrainCatalog",
      "git_repo": "",
      "description": "Provides an immutable list of Train objects via a static getTrains() method. Depends on the Train class but has no other project‑level dependents.",
      "relevant_files": [],
      "complexity": "medium",
      "complexity_score": 4,
      "complexity_justification": "Requires the Train class to be present, adding a modest amount of integration work, but still only uses standard‑library types.",
      "rank_justification": "After Train is migrated, TrainCatalog can be updated safely; its single dependency makes it the next logical step.",
      "rhel10_compatibility_issues": "Same considerations as Train: avoid SHA‑1 usage and ensure compatibility with the upgraded JDK. No direct OS‑level incompatibilities.",
      "rhel10_compatibility_issues_reference_sources": [
        "BREAKING CHANGES AND INCOMPATIBILITIES – Cryptography (SHA-1)",
        "RUNTIME AND MIDDLEWARE CONSIDERATIONS – Java"
      ],
      "runtime_upgrade_required": "JDK 8 → JDK 21"
    },
    {
      "migration_order": 3,
      "component": "TrainApp",
      "git_repo": "",
      "description": "Application entry point with a private constructor and static main method; calls TrainCatalog.getTrains() to drive the program.",
      "relevant_files": [],
      "complexity": "medium",
      "complexity_score": 5,
      "complexity_justification": "Depends on TrainCatalog (and transitively on Train). No additional external libraries, but being the executable component adds a slight increase in risk.",
      "rank_justification": "Once the catalog is stable, migrating the entry point ensures the whole runtime flow is consistent with the new codebase.",
      "rhel10_compatibility_issues": "Executable must run under the target JDK (JDK 21). Verify that any command‑line handling or file I/O does not rely on legacy APIs that were removed or deprecated in newer Java releases. Ensure no SHA‑1 based verification is performed at runtime.",
      "rhel10_compatibility_issues_reference_sources": [
        "BREAKING CHANGES AND INCOMPATIBILITIES – Cryptography (SHA-1)",
        "RUNTIME AND MIDDLEWARE CONSIDERATIONS – Java"
      ],
      "runtime_upgrade_required": "JDK 8 → JDK 21"
    },
    {
      "migration_order": 4,
      "component": "Maven build configuration",
      "git_repo": "",
      "description": "Defines the build lifecycle, packaging, and mainClass. It does not affect runtime code directly, making it a low‑risk change, though it must be aligned with the migrated classes.",
      "relevant_files": [],
      "complexity": "low",
      "complexity_score": 3,
      "complexity_justification": "No code changes, only build‑tool configuration; the only complexity is ensuring plugin versions remain compatible with the target runtime.",
      "rank_justification": "Updating the build after the core classes are migrated avoids unnecessary rebuild failures and keeps the build process in sync.",
      "rhel10_compatibility_issues": "Maven plugins and dependencies must support JDK 21 and the libraries provided by RHEL 10. Verify that any plugin that invokes system tools (e.g., exec‑plugin) does not rely on legacy network‑scripts or OS version checks.",
      "rhel10_compatibility_issues_reference_sources": [
        "RUNTIME AND MIDDLEWARE CONSIDERATIONS – Java",
        "BREAKING CHANGES AND INCOMPATIBILITIES – Hard‑coded OS version checks"
      ],
      "runtime_upgrade_required": "Maven plugins compatible with JDK 21"
    },
    {
      "migration_order": 5,
      "component": "Repository metadata",
      "git_repo": "",
      "description": "Top‑level artifact that aggregates all modules and has the most dependents. It should be migrated last to avoid breaking downstream components.",
      "relevant_files": [],
      "complexity": "high",
      "complexity_score": 7,
      "complexity_justification": "Changing the overall project structure can impact many downstream consumers; careful coordination is required, especially if the structure influences CI/CD pipelines.",
      "rank_justification": "As the component with the greatest number of dependents, it is safest to migrate it after all internal code and build configuration have been stabilized.",
      "rhel10_compatibility_issues": "CI/CD scripts, deployment manifests, or any automation that checks for RHEL 8 version strings must be updated. Ensure SELinux policies and any fapolicyd rules used in pipelines are compatible with RHEL 10. Verify that no network‑scripts are referenced in deployment automation.",
      "rhel10_compatibility_issues_reference_sources": [
        "BREAKING CHANGES AND INCOMPATIBILITIES – Hard‑coded OS version checks",
        "SECURITY CHANGES – SELinux",
        "BREAKING CHANGES AND INCOMPATIBILITIES – Network Configuration"
      ]
    }
  ]
}
```

### End-to-End RHEL Migration Plan (JSON)

```json
{
    "end_to_end_migration_plan": [
        {
            "rank": 1,
            "phase": "Initial Assessment & Inventory",
            "description": "Collect hardware, OS, subscription, and application inventory. Verify architecture compatibility (x86‑64‑v3, POWER9, etc.), identify all installed packages, and run a code‑base scan for SHA‑1 usage, network‑scripts, Stratis, JBoss, and other breaking‑change indicators.",
            "complexity": "low",
            "complexity_score": 2,
            "complexity_justification": "Mostly documentation and automated inventory commands; no system changes required.",
            "rhel10_compatibility_issues": "Potential presence of SHA‑1 signed packages, legacy network‑scripts, Stratis volumes, or JBoss EAP that would block the upgrade.",
            "rhel10_compatibility_issues_reference_sources": [
                "BREAKING CHANGES AND INCOMPATIBILITIES – Cryptography (SHA-1)",
                "BREAKING CHANGES AND INCOMPATIBILITIES – Network Configuration",
                "BREAKING CHANGES AND INCOMPATIBILITIES – Stratis Filesystem",
                "BREAKING CHANGES AND INCOMPATIBILITIES – JBoss Enterprise Application Platform (EAP)"
            ]
        },
        {
            "rank": 2,
            "phase": "Backup & Snapshot",
            "description": "Create full system backups using ReaR, LVM snapshots, or VM snapshots. Verify backup integrity and store copies off‑site. This provides a rollback point for each upgrade hop.",
            "complexity": "low",
            "complexity_score": 2,
            "complexity_justification": "Standard backup procedures; requires storage planning but no complex logic.",
            "rhel10_compatibility_issues": "None specific to RHEL 10, but backups must be restorable on the target OS version.",
            "rhel10_compatibility_issues_reference_sources": [
                "PRE-UPGRADE CHECKLIST – Back up the system"
            ]
        },
        {
            "rank": 3,
            "phase": "Resolve Pre‑Upgrade Inhibitors",
            "description": "Address all leapp preupgrade inhibitors: replace network‑scripts with NetworkManager dispatcher units, remove or re‑sign SHA‑1 packages, migrate any Stratis volumes to XFS/LVM, and uninstall Ansible Automation Platform if present. Clear dnf versionlock and disable antivirus/watchdogs.",
            "complexity": "medium",
            "complexity_score": 5,
            "complexity_justification": "Requires manual remediation of multiple potential blockers; impact varies per system.",
            "rhel10_compatibility_issues": "Inhibitors such as legacy network‑scripts, SHA‑1 signatures, Stratis, and AAP would prevent the Leapp upgrade.",
            "rhel10_compatibility_issues_reference_sources": [
                "PRE-UPGRADE CHECKLIST – Resolve all inhibitors found in the leapp preupgrade report",
                "BREAKING CHANGES AND INCOMPATIBILITIES – Network Configuration",
                "BREAKING CHANGES AND INCOMPATIBILITIES – Cryptography (SHA-1)",
                "BREAKING CHANGES AND INCOMPATIBILITIES – Stratis Filesystem",
                "BREAKING CHANGES AND INCOMPATIBILITIES – Ansible Automation Platform"
            ]
        },
        {
            "rank": 4,
            "phase": "Runtime Stack Upgrade – Java",
            "description": "Migrate the application from JDK 1.8 to OpenJDK 21 (RHEL 10 default). Update the Maven `pom.xml` to set `maven-compiler-plugin` source/target to 21, adjust any code that uses removed APIs, and rebuild the JAR. Verify with unit/integration tests.",
            "complexity": "high",
            "complexity_score": 7,
            "complexity_justification": "Code changes may be required due to API deprecations; testing across Java versions adds effort.",
            "rhel10_compatibility_issues": "RHEL 10 does not provide JDK 8 packages; application will fail to start with the older runtime.",
            "rhel10_compatibility_issues_reference_sources": [
                "RUNTIME AND MIDDLEWARE CONSIDERATIONS – Java",
                "IMPLICATIONS FOR CODE ANALYSIS – Java version"
            ]
        },
        {
            "rank": 5,
            "phase": "RHEL 8 → RHEL 9 Upgrade Hop",
            "description": "Execute the first Leapp upgrade: register with RHSM, enable RHEL 9 repositories, run `leapp preupgrade`, resolve any new inhibitors, then run `leapp upgrade`. Reboot into RHEL 9.6 (EUS) or 9.8 as chosen. Perform post‑upgrade validation (service start‑up, SELinux enforcing, FIPS status).",
            "complexity": "high",
            "complexity_score": 8,
            "complexity_justification": "System‑wide OS change with potential driver, kernel, and service adjustments; requires careful validation.",
            "rhel10_compatibility_issues": "Inhibitors from the first hop (e.g., leftover SHA‑1 packages, network‑scripts) must be cleared; SELinux will be in permissive mode during upgrade.",
            "rhel10_compatibility_issues_reference_sources": [
                "UPGRADE PATH OVERVIEW – Hop 1",
                "UPGRADE TOOLING – Leapp commands",
                "SECURITY CHANGES – SELinux"
            ]
        },
        {
            "rank": 6,
            "phase": "Post‑RHEL 9 Validation",
            "description": "Confirm that all system services start correctly, SELinux is set back to enforcing, cryptographic policies are applied, and any required kernel modules are loaded. Update `fapolicyd` and USBGuard databases, and run CIS/DISA STIG hardening checks.",
            "complexity": "medium",
            "complexity_score": 4,
            "complexity_justification": "Standard validation tasks but must be thorough to avoid issues in the next hop.",
            "rhel10_compatibility_issues": "None new; this step ensures a clean baseline before the second upgrade.",
            "rhel10_compatibility_issues_reference_sources": [
                "POST‑UPGRADE security tasks include: Restore SELinux to enforcing mode"
            ]
        },
        {
            "rank": 7,
            "phase": "RHEL 9 → RHEL 10 Upgrade Hop",
            "description": "Perform the second Leapp upgrade: ensure RHSM points to RHEL 10 repositories (10.0 EUS if on 9.6, or 10.2 if on 9.8), run `leapp preupgrade`, resolve any remaining inhibitors (e.g., JBoss EAP not supported for in‑place upgrade), then run `leapp upgrade`. Reboot into RHEL 10 and verify boot loader compatibility (BIOS/UEFI unchanged).",
            "complexity": "high",
            "complexity_score": 8,
            "complexity_justification": "Second major OS transition with stricter cryptographic policies and additional deprecations; requires careful inhibitor handling.",
            "rhel10_compatibility_issues": "JBoss EAP must be reinstalled manually after upgrade; Stratis volumes are unsupported; any remaining SHA‑1 or network‑scripts would block the upgrade.",
            "rhel10_compatibility_issues_reference_sources": [
                "UPGRADE PATH OVERVIEW – Hop 2",
                "BREAKING CHANGES AND INCOMPATIBILITIES – JBoss Enterprise Application Platform (EAP)",
                "BREAKING CHANGES AND INCOMPATIBILITIES – Stratis Filesystem",
                "BREAKING CHANGES AND INCOMPATIBILITIES – Cryptography (SHA-1)"
            ]
        },
        {
            "rank": 8,
            "phase": "Post‑RHEL 10 Validation & Hardening",
            "description": "Re‑enable SELinux enforcing, verify FIPS mode persistence, update `fapolicyd` and USBGuard, run security baselines (CIS/DISA STIG), and confirm that all kernel modules and drivers load correctly on RHEL 10.",
            "complexity": "medium",
            "complexity_score": 4,
            "complexity_justification": "Security hardening is routine but must be repeated after the second upgrade.",
            "rhel10_compatibility_issues": "None specific; ensures the system complies with RHEL 10 security expectations.",
            "rhel10_compatibility_issues_reference_sources": [
                "SECURITY CHANGES – Post‑upgrade security tasks"
            ]
        },
        {
            "rank": 9,
            "phase": "Application Rebuild, Test & Deploy",
            "description": "Using the upgraded OpenJDK 21, rebuild the Maven project, run unit/integration tests, and perform functional validation (e.g., verify catalog output). Package the JAR for deployment, update any service wrappers or systemd unit files if needed, and stage the application on a test environment running RHEL 10.",
            "complexity": "medium",
            "complexity_score": 6,
            "complexity_justification": "Requires recompilation and testing but the codebase is small and has no external dependencies.",
            "rhel10_compatibility_issues": "Application must run on Java 21; any residual use of removed APIs will cause runtime failures.",
            "rhel10_compatibility_issues_reference_sources": [
                "RUNTIME AND MIDDLEWARE CONSIDERATIONS – Java",
                "IMPLICATIONS FOR CODE ANALYSIS – Java version"
            ]
        },
        {
            "rank": 10,
            "phase": "Production Cutover & Monitoring",
            "description": "Switch traffic to the newly built application on RHEL 10, monitor logs, performance, and health checks for a defined stabilization period. Keep the previous RHEL 8/9 environment available for rollback until the cutover is confirmed successful.",
            "complexity": "medium",
            "complexity_score": 5,
            "complexity_justification": "Operational coordination and monitoring; risk is limited by prior validation steps.",
            "rhel10_compatibility_issues": "Potential runtime issues not caught in testing (e.g., locale or timezone differences).",
            "rhel10_compatibility_issues_reference_sources": [
                "POST‑UPGRADE security tasks include: Verify USBGuard policies"
            ]
        }
    ]
}
```
