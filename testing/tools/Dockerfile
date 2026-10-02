# The image the workload tests (testing/workload) run in: Neon's build-tools image (Python and Poetry for the tests),
# with a Java runtime, SQLancer and BenchBase.
#
# Built by .github/workflows/neon-test.yml as ghcr.io/jclab-oss/neon-test-tools:<hash of this file>-<arch>, when
# missing.

ARG BUILD_TOOLS_IMAGE=ghcr.io/neondatabase/build-tools:pinned-bookworm

FROM maven:3.9-eclipse-temurin-25 AS build
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /build

# https://github.com/sqlancer/sqlancer
ARG SQLANCER_COMMIT=d8e6b158a1a18ab803d1c90bbbc45f58c94c60ba
RUN git init -q sqlancer && cd sqlancer \
    && git fetch -q --depth 1 https://github.com/sqlancer/sqlancer "${SQLANCER_COMMIT}" && git checkout -q FETCH_HEAD \
    # `--test-tablespaces false` doesn't disable the tablespace tests on Linux, where they're on by default: make them
    # off unless `--test-tablespaces true` (on Linux)
    && sed -i 's/return testTablespaces || getDefaultTablespaceSupport();/return testTablespaces \&\& getDefaultTablespaceSupport();/' \
        src/sqlancer/postgres/PostgresOptions.java \
    && grep -q 'return testTablespaces && getDefaultTablespaceSupport();' src/sqlancer/postgres/PostgresOptions.java \
    && mvn -B -q package -DskipTests \
    && mkdir /opt/sqlancer && cp target/sqlancer-*.jar /opt/sqlancer/sqlancer.jar && cp -r target/lib /opt/sqlancer/

# https://github.com/cmu-db/benchbase
ARG BENCHBASE_COMMIT=33c00473807ebd49304d114a6d769d2d2b2bbb34
RUN git init -q benchbase && cd benchbase \
    && git fetch -q --depth 1 https://github.com/cmu-db/benchbase "${BENCHBASE_COMMIT}" && git checkout -q FETCH_HEAD \
    # It is built with JDK 23 upstream: JDK 25's new warnings would fail it (-Werror), and its formatter plugin
    # doesn't run on JDK 25
    && sed -i '/<arg>-Werror<\/arg>/d' pom.xml \
    # (not ./mvnw, which passes the image's MAVEN_CONFIG to Maven as an argument)
    && mvn -B -q package -P postgres -DskipTests -Dfmt.skip=true \
    && tar -xzf target/benchbase-postgres.tgz -C /opt && mv /opt/benchbase-postgres /opt/benchbase

FROM eclipse-temurin:25-jre AS jre

FROM ${BUILD_TOOLS_IMAGE}
LABEL org.opencontainers.image.source=https://github.com/jclab-oss/neon-information
COPY --from=jre /opt/java/openjdk /opt/java
COPY --from=build /opt/sqlancer /opt/sqlancer
COPY --from=build /opt/benchbase /opt/benchbase
ENV JAVA_HOME=/opt/java \
    SQLANCER_JAR=/opt/sqlancer/sqlancer.jar \
    BENCHBASE_HOME=/opt/benchbase
