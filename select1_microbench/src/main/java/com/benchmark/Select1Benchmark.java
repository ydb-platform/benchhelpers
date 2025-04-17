package com.benchmark;

import org.apache.commons.cli.*;
import org.apache.commons.math3.stat.descriptive.rank.Percentile;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

public class Select1Benchmark {
    private static final String DEFAULT_PORT = "5432";
    private static final int DEFAULT_MIN_INFLIGHT = 1;
    private static final int DEFAULT_MAX_INFLIGHT = 64;
    private static final int DEFAULT_INTERVAL_SECONDS = 5;
    private static final String DEFAULT_FORMAT = "human";

    private enum OutputFormat {
        HUMAN,
        CSV
    }

    private static class BenchmarkResult {
        final int inflight;
        final double rps;
        final double p50;
        final double p90;
        final double p99;
        final double p999;

        BenchmarkResult(int inflight, double rps, double p50, double p90, double p99, double p999) {
            this.inflight = inflight;
            this.rps = rps;
            this.p50 = p50;
            this.p90 = p90;
            this.p99 = p99;
            this.p999 = p999;
        }

        @Override
        public String toString() {
            return String.format("Inflight: %d, RPS: %.2f, P50: %.2fµs, P90: %.2fµs, P99: %.2fµs, P99.9: %.2fµs",
                    inflight, rps, p50, p90, p99, p999);
        }
    }

    private static void printCSV(List<BenchmarkResult> results) {
        System.out.print("What");
        for (BenchmarkResult result : results) {
            System.out.printf(",%d", result.inflight);
        }
        System.out.println();

        System.out.print("Latency p50 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.p50);
        }
        System.out.println();

        System.out.print("Latency p90 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.p90);
        }
        System.out.println();

        System.out.print("Latency p99 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.p99);
        }
        System.out.println();

        System.out.print("Latency p99.9 (µs)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.p999);
        }
        System.out.println();

        System.out.print("Throughput (RPS)");
        for (BenchmarkResult result : results) {
            System.out.printf(",%.2f", result.rps);
        }
        System.out.println();
    }

    private static void printHuman(List<BenchmarkResult> results) {
        // Calculate column widths
        int inflightWidth = Math.max(8, String.valueOf(results.get(results.size() - 1).inflight).length());
        int rpsWidth = Math.max(12, results.stream()
                .mapToInt(r -> String.format("%.2f", r.rps).length())
                .max()
                .orElse(12));
        int latencyWidth = Math.max(10, results.stream()
                .flatMapToInt(r -> Stream.of(r.p50, r.p90, r.p99, r.p999)
                        .mapToInt(v -> String.format("%.2f", v).length()))
                .max()
                .orElse(10));

        // Print header
        String headerFormat = "%-" + inflightWidth + "s  %-" + rpsWidth + "s  %-" + latencyWidth + "s  %-" + latencyWidth + "s  %-" + latencyWidth + "s  %-" + latencyWidth + "s%n";
        System.out.println("\nBenchmark Results");
        System.out.println("=".repeat(inflightWidth + rpsWidth + latencyWidth * 4 + 10));
        System.out.printf(headerFormat, "Inflight", "RPS", "P50 (µs)", "P90 (µs)", "P99 (µs)", "P99.9 (µs)");
        System.out.println("-".repeat(inflightWidth + rpsWidth + latencyWidth * 4 + 10));

        // Print results
        String rowFormat = "%-" + inflightWidth + "d  %" + rpsWidth + ".2f  %" + latencyWidth + ".2f  %" + latencyWidth + ".2f  %" + latencyWidth + ".2f  %" + latencyWidth + ".2f%n";
        for (BenchmarkResult result : results) {
            System.out.printf(rowFormat,
                    result.inflight,
                    result.rps,
                    result.p50,
                    result.p90,
                    result.p99,
                    result.p999);
        }
        System.out.println("=".repeat(inflightWidth + rpsWidth + latencyWidth * 4 + 10));
    }

    private static void printResults(List<BenchmarkResult> results, OutputFormat format) {
        if (format == OutputFormat.CSV) {
            printCSV(results);
        } else {
            printHuman(results);
        }
    }

    private static class QueryRunner implements Callable<List<Long>> {
        private final String url;
        private final long durationNanos;
        private final AtomicInteger completed;
        private final AtomicInteger errors;

        QueryRunner(String url, long durationNanos, AtomicInteger completed, AtomicInteger errors) {
            this.url = url;
            this.durationNanos = durationNanos;
            this.completed = completed;
            this.errors = errors;
        }

        @Override
        public List<Long> call() throws Exception {
            List<Long> latencies = new ArrayList<>();
            Connection conn = null;
            PreparedStatement stmt = null;
            try {
                conn = DriverManager.getConnection(url);
                stmt = conn.prepareStatement("SELECT 1");
                long startTime = System.nanoTime();
                while (System.nanoTime() - startTime < durationNanos) {
                    long start = System.nanoTime();
                    try (ResultSet rs = stmt.executeQuery()) {
                        if (rs.next()) {
                            long end = System.nanoTime();
                            latencies.add(end - start);
                            completed.incrementAndGet();
                        }
                    } catch (Exception e) {
                        System.err.printf("Error executing query: %s%n", e.getMessage());
                        errors.incrementAndGet();
                        // Sleep a bit to avoid tight error loop
                        Thread.sleep(100);
                    }
                }
            } catch (Exception e) {
                System.err.printf("Error in query runner: %s%n", e.getMessage());
                errors.incrementAndGet();
                throw e;
            } finally {
                if (stmt != null) {
                    try {
                        stmt.close();
                    } catch (Exception e) {
                        System.err.printf("Error closing statement: %s%n", e.getMessage());
                    }
                }
                if (conn != null) {
                    try {
                        conn.close();
                    } catch (Exception e) {
                        System.err.printf("Error closing connection: %s%n", e.getMessage());
                    }
                }
            }
            return latencies;
        }
    }

    private static BenchmarkResult runBenchmark(String url, int inflight, int intervalSeconds) throws Exception {
        ExecutorService executor = Executors.newFixedThreadPool(inflight);
        List<Future<List<Long>>> futures = new ArrayList<>(inflight);
        AtomicInteger completed = new AtomicInteger(0);
        AtomicInteger errors = new AtomicInteger(0);
        long startTime = System.nanoTime();

        try {
            for (int i = 0; i < inflight; i++) {
                futures.add(executor.submit(new QueryRunner(url, intervalSeconds * 1_000_000_000L, completed, errors)));
            }

            List<Long> allLatencies = new ArrayList<>();
            for (Future<List<Long>> future : futures) {
                try {
                    allLatencies.addAll(future.get());
                } catch (Exception e) {
                    System.err.printf("Error getting future result: %s%n", e.getMessage());
                }
            }

            long endTime = System.nanoTime();
            executor.shutdown();
            if (!executor.awaitTermination(1, TimeUnit.MINUTES)) {
                System.err.println("Timeout waiting for executor shutdown");
                executor.shutdownNow();
            }

            double durationSeconds = (endTime - startTime) / 1e9;
            double rps = completed.get() / durationSeconds;

            if (allLatencies.isEmpty()) {
                throw new RuntimeException("No successful queries completed");
            }

            double[] latencies = allLatencies.stream().mapToDouble(l -> l / 1e3).toArray();
            Percentile percentile = new Percentile();
            double p50 = percentile.evaluate(latencies, 50);
            double p90 = percentile.evaluate(latencies, 90);
            double p99 = percentile.evaluate(latencies, 99);
            double p999 = percentile.evaluate(latencies, 99.9);

            if (errors.get() > 0) {
                System.err.printf("Warning: %d errors occurred during benchmark%n", errors.get());
            }

            return new BenchmarkResult(inflight, rps, p50, p90, p99, p999);
        } catch (Exception e) {
            executor.shutdownNow();
            throw e;
        }
    }

    private static Options createOptions() {
        Options options = new Options();
        options.addOption(Option.builder("j")
                .longOpt("jdbc-url")
                .hasArg()
                .desc("JDBC connection URL (overrides host/port/user/password options)")
                .build());
        options.addOption(Option.builder("h")
                .longOpt("host")
                .hasArg()
                .desc("PostgreSQL server hostname (default: localhost)")
                .build());
        options.addOption(Option.builder("p")
                .longOpt("port")
                .hasArg()
                .desc("PostgreSQL server port (default: " + DEFAULT_PORT + ")")
                .build());
        options.addOption(Option.builder("u")
                .longOpt("user")
                .hasArg()
                .desc("PostgreSQL username (default: postgres)")
                .build());
        options.addOption(Option.builder("w")
                .longOpt("password")
                .hasArg()
                .desc("PostgreSQL password (default: postgres)")
                .build());
        options.addOption(Option.builder("m")
                .longOpt("min-inflight")
                .hasArg()
                .desc("Minimum number of concurrent connections (default: " + DEFAULT_MIN_INFLIGHT + ")")
                .build());
        options.addOption(Option.builder("M")
                .longOpt("max-inflight")
                .hasArg()
                .desc("Maximum number of concurrent connections (default: " + DEFAULT_MAX_INFLIGHT + ")")
                .build());
        options.addOption(Option.builder("i")
                .longOpt("interval")
                .hasArg()
                .desc("Duration in seconds to run each inflight level (default: " + DEFAULT_INTERVAL_SECONDS + ")")
                .build());
        options.addOption(Option.builder("f")
                .longOpt("format")
                .hasArg()
                .desc("Output format: human or csv (default: " + DEFAULT_FORMAT + ")")
                .build());
        return options;
    }

    public static void main(String[] args) {
        Options options = createOptions();
        CommandLineParser parser = new DefaultParser();
        HelpFormatter formatter = new HelpFormatter();

        try {
            CommandLine cmd = parser.parse(options, args);

            String jdbcUrl = cmd.getOptionValue("jdbc-url");
            if (jdbcUrl == null) {
                String hostname = cmd.getOptionValue("host", "localhost");
                String port = cmd.getOptionValue("port", DEFAULT_PORT);
                String username = cmd.getOptionValue("user", "postgres");
                String password = cmd.getOptionValue("password", "postgres");
                jdbcUrl = String.format("jdbc:postgresql://%s:%s/postgres?user=%s&password=%s", 
                        hostname, port, username, password);
            }

            if (jdbcUrl.startsWith("jdbc:ydb:")) {
                Class.forName("tech.ydb.jdbc.YdbDriver");
            } else if (jdbcUrl.startsWith("jdbc:postgresql:")) {
                Class.forName("org.postgresql.Driver");
            }

            int minInflight = Integer.parseInt(cmd.getOptionValue("min-inflight", String.valueOf(DEFAULT_MIN_INFLIGHT)));
            int maxInflight = Integer.parseInt(cmd.getOptionValue("max-inflight", String.valueOf(DEFAULT_MAX_INFLIGHT)));
            int intervalSeconds = Integer.parseInt(cmd.getOptionValue("interval", String.valueOf(DEFAULT_INTERVAL_SECONDS)));
            String formatStr = cmd.getOptionValue("format", DEFAULT_FORMAT).toLowerCase();
            OutputFormat format;
            try {
                format = OutputFormat.valueOf(formatStr.toUpperCase());
            } catch (IllegalArgumentException e) {
                System.err.println("Invalid format: " + formatStr + ". Must be 'human' or 'csv'");
                System.exit(1);
                return;
            }

            System.out.println("Running benchmark with parameters:");
            System.out.printf("JDBC URL: %s, Min Inflight: %d, Max Inflight: %d, Interval: %d seconds, Format: %s%n",
                    jdbcUrl, minInflight, maxInflight, intervalSeconds, formatStr);

            List<BenchmarkResult> results = new ArrayList<>();
            for (int inflight = minInflight; inflight <= maxInflight; inflight++) {
                results.add(runBenchmark(jdbcUrl, inflight, intervalSeconds));
            }

            printResults(results, format);
        } catch (ParseException e) {
            System.err.println(e.getMessage());
            formatter.printHelp("select1-benchmark", options);
            System.exit(1);
        } catch (Exception e) {
            System.err.println("Error running benchmark: " + e.getMessage());
            e.printStackTrace();
            System.exit(1);
        }
    }
} 