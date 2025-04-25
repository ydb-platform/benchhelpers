#include <algorithm>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <stop_token>
#include <string>
#include <thread>
#include <vector>

#include <grpcpp/grpcpp.h>

#include "debug.pb.h"
#include "debug.grpc.pb.h"

using grpc::Channel;
using grpc::ClientContext;
using grpc::Status;
using Ydb::Debug::V1::DebugService;
using Ydb::Debug::V1::PlainGrpcRequest;
using Ydb::Debug::V1::PlainGrpcResponse;

class DebugServiceClient {
public:
    DebugServiceClient(std::shared_ptr<Channel> channel)
        : stub_(DebugService::NewStub(channel)), channel_(channel) {}

    uint64_t Ping() {
        PlainGrpcRequest request;
        PlainGrpcResponse response;
        ClientContext context;

        // Set a timeout of 1 second
        std::chrono::system_clock::time_point deadline = 
            std::chrono::system_clock::now() + std::chrono::seconds(1);
        context.set_deadline(deadline);

        // Check channel state before making the request
        auto state = channel_->GetState(false);
        if (state != GRPC_CHANNEL_READY) {
            channel_->WaitForConnected(std::chrono::system_clock::now() + std::chrono::seconds(1));
            state = channel_->GetState(false);
            if (state != GRPC_CHANNEL_READY) {
                std::cerr << "Failed to connect to server. Channel state: " << state << std::endl;
                std::exit(1);
            }
        }

        auto start = std::chrono::high_resolution_clock::now();
        Status status = stub_->PingPlainGrpc(&context, request, &response);
        auto end = std::chrono::high_resolution_clock::now();

        if (!status.ok()) {
            std::cerr << "RPC failed: " 
                      << "code=" << status.error_code() 
                      << " message=\"" << status.error_message() << "\""
                      << " details=\"" << status.error_details() << "\""
                      << std::endl;
            
            std::cerr << "Channel state: " << channel_->GetState(false) << std::endl;
            std::cerr << "Service name: Ydb.Debug.V1.DebugService" << std::endl;
            std::cerr << "Method name: PingPlainGrpc" << std::endl;
            std::cerr << "Full method path: /Ydb.Debug.V1.DebugService/PingPlainGrpc" << std::endl;
            
            if (status.error_code() == grpc::StatusCode::DEADLINE_EXCEEDED) {
                std::cerr << "Error: Request timed out after 1 second" << std::endl;
            } else if (status.error_code() == grpc::StatusCode::UNIMPLEMENTED) {
                std::cerr << "Error: Method PingPlainGrpc is not implemented on the server" << std::endl;
                std::cerr << "Please check if the server has the DebugService with PingPlainGrpc method implemented" << std::endl;
            }
            std::exit(1);
        }

        return std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();
    }

private:
    std::unique_ptr<DebugService::Stub> stub_;
    std::shared_ptr<Channel> channel_;
};

struct alignas(64) PerThreadResult {
    std::vector<uint64_t> latencies;
};

void Worker(std::string host, int port, std::stop_token stop_token, std::atomic<bool>& startMeasure, PerThreadResult& result) {
    std::string target = host + ":" + std::to_string(port);
    
    auto channel = grpc::CreateChannel(target, grpc::InsecureChannelCredentials());
    DebugServiceClient client(channel);

    // warmup
    while (!startMeasure.load(std::memory_order_relaxed) && !stop_token.stop_requested()) {
        client.Ping();
    }

    // measure
    while (!stop_token.stop_requested()) {
        uint64_t latency = client.Ping();
        result.latencies.push_back(latency);
    }
}

void PrintStats(const std::vector<uint64_t>& latencies, int total_requests, int interval_seconds) {
    if (latencies.empty()) {
        std::cout << "No successful requests" << std::endl;
        return;
    }

    std::vector<uint64_t> sorted_latencies = latencies;
    std::sort(sorted_latencies.begin(), sorted_latencies.end());

    auto percentile = [&](double p) -> uint64_t {
        size_t index = static_cast<size_t>(p * sorted_latencies.size());
        return sorted_latencies[index];
    };

    double throughput = static_cast<double>(total_requests) / interval_seconds;

    std::cout << std::fixed << std::setprecision(2);
    std::cout << "Throughput: " << throughput << " req/s" << std::endl;
    std::cout << "Latency percentiles (us):" << std::endl;
    std::cout << "  50th: " << percentile(0.50) << std::endl;
    std::cout << "  90th: " << percentile(0.90) << std::endl;
    std::cout << "  99th: " << percentile(0.99) << std::endl;
}

void PrintUsage(const char* program_name) {
    std::cout << "Usage: " << program_name << " [options]" << std::endl;
    std::cout << "Options:" << std::endl;
    std::cout << "  -h, --help           Show this help message" << std::endl;
    std::cout << "  --host <hostname>    Server hostname (default: localhost)" << std::endl;
    std::cout << "  --port <port>        Server port (default: 2137)" << std::endl;
    std::cout << "  --inflight <N>       Number of concurrent requests (default: 32)" << std::endl;
    std::cout << "  --interval <seconds> Benchmark duration in seconds (default: 10)" << std::endl;
    std::cout << "  --warmup <seconds>   Warmup duration in seconds (default: 1)" << std::endl;
}

int main(int argc, char** argv) {
    std::string host = "localhost";
    int port = 2137;
    int inflight = 32;
    int interval_seconds = 10;
    int warmup_seconds = 1;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "-h" || arg == "--help") {
            PrintUsage(argv[0]);
            return 0;
        } else if (arg == "--host" && i + 1 < argc) {
            host = argv[++i];
        } else if (arg == "--port" && i + 1 < argc) {
            port = std::stoi(argv[++i]);
        } else if (arg == "--inflight" && i + 1 < argc) {
            inflight = std::stoi(argv[++i]);
        } else if (arg == "--interval" && i + 1 < argc) {
            interval_seconds = std::stoi(argv[++i]);
        } else if (arg == "--warmup" && i + 1 < argc) {
            warmup_seconds = std::stoi(argv[++i]);
        } else {
            std::cerr << "Unknown option: " << arg << std::endl;
            PrintUsage(argv[0]);
            return 1;
        }
    }

    std::vector<std::jthread> threads;
    std::vector<PerThreadResult> thread_results(inflight);
    std::stop_source stop_source;
    std::atomic<bool> startMeasure{false};

    // Start worker threads
    for (int i = 0; i < inflight; ++i) {
        threads.emplace_back(Worker, 
            std::string(host), // Pass by value
            port,
            stop_source.get_token(),
            std::ref(startMeasure),
            std::ref(thread_results[i])
        );
    }

    // Wait for all threads to start and warmup
    std::cout << "Warmup phase started..." << std::endl;
    std::this_thread::sleep_for(std::chrono::seconds(warmup_seconds));
    std::cout << "Warmup phase completed, measuring..." << std::endl;

    startMeasure.store(true, std::memory_order_relaxed);

    auto start = std::chrono::high_resolution_clock::now();
    std::this_thread::sleep_for(std::chrono::seconds(interval_seconds));

    stop_source.request_stop();
    for (auto& thread : threads) {
        thread.join();
    }

    auto end = std::chrono::high_resolution_clock::now();
    auto total_time = std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();

    std::vector<uint64_t> all_latencies;
    for (const auto& result: thread_results) {
        all_latencies.insert(all_latencies.end(), result.latencies.begin(), result.latencies.end());
    }

    std::cout << "Total requests: " << all_latencies.size() << std::endl;
    std::cout << "Total time: " << total_time << " us" << std::endl;
    PrintStats(all_latencies, all_latencies.size(), interval_seconds);

    return 0;
}
