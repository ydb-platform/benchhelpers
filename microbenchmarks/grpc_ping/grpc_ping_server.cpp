#include <grpcpp/grpcpp.h>
#include <grpcpp/alarm.h>
#include <grpcpp/server_builder.h>
#include <grpcpp/server_context.h>

#include <atomic>
#include <chrono>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "debug.grpc.pb.h"
#include "debug.pb.h"

using grpc::Server;
using grpc::ServerAsyncResponseWriter;
using grpc::ServerBuilder;
using grpc::ServerCompletionQueue;
using grpc::ServerContext;
using grpc::Status;

using Ydb::Debug::V1::DebugService;
using Ydb::Debug::V1::PlainGrpcRequest;
using Ydb::Debug::V1::PlainGrpcResponse;

class ServerImpl final {
public:
    ServerImpl(const std::string& address, int num_cqs, int workers_per_cq, int callbacks_per_cq)
        : address_(address)
        , num_cqs_(num_cqs)
        , workers_per_cq_(workers_per_cq)
        , callbacks_per_cq_(callbacks_per_cq) {}

    ~ServerImpl() {
        server_->Shutdown();
        for (auto& cq : cqs_) {
            cq->Shutdown();
        }
    }

    void Run() {
        ServerBuilder builder;
        builder.AddListeningPort(address_, grpc::InsecureServerCredentials());
        builder.RegisterService(&service_);

        // Create completion queues
        for (int i = 0; i < num_cqs_; i++) {
            cqs_.emplace_back(builder.AddCompletionQueue());
        }

        server_ = builder.BuildAndStart();
        std::cout << "Server listening on " << address_ << std::endl;
        std::cout << "Configuration: " << num_cqs_ << " CQs, " 
                  << workers_per_cq_ << " workers per CQ, "
                  << callbacks_per_cq_ << " callbacks per CQ" << std::endl;

        // Start worker threads for each CQ
        for (int i = 0; i < num_cqs_; i++) {
            for (int j = 0; j < workers_per_cq_; j++) {
                threads_.emplace_back([this, i]() { HandleRpcs(i); });
            }
        }

        // Create initial CallData instances for each CQ
        for (int i = 0; i < num_cqs_; i++) {
            for (int j = 0; j < callbacks_per_cq_; j++) {
                new CallData(&service_, cqs_[i].get());
            }
        }

        // Wait for all threads to complete
        for (auto& thread : threads_) {
            thread.join();
        }
    }

private:
    class CallData {
    public:
        CallData(DebugService::AsyncService* service, ServerCompletionQueue* cq)
            : service_(service)
            , cq_(cq)
            , responder_(&ctx_)
            , status_(CREATE) {
            Proceed();
        }

        void Proceed() {
            if (status_ == CREATE) {
                status_ = PROCESS;
                service_->RequestPingPlainGrpc(&ctx_, &request_, &responder_, cq_, cq_, this);
            } else if (status_ == PROCESS) {
                // Create a new CallData instance to handle the next request
                new CallData(service_, cq_);

                status_ = FINISH;
                responder_.Finish(response_, Status::OK, this);
            } else {
                delete this;
            }
        }

    private:
        DebugService::AsyncService* service_;
        ServerCompletionQueue* cq_;
        ServerContext ctx_;
        PlainGrpcRequest request_;
        PlainGrpcResponse response_;
        ServerAsyncResponseWriter<PlainGrpcResponse> responder_;

        enum CallStatus { CREATE, PROCESS, FINISH };
        CallStatus status_;
    };

    void HandleRpcs(int cq_index) {
        void* tag;
        bool ok;

        while (true) {
            if (!cqs_[cq_index]->Next(&tag, &ok)) {
                break;
            }

            if (ok) {
                static_cast<CallData*>(tag)->Proceed();
            }
        }
    }

    std::string address_;
    int num_cqs_;
    int workers_per_cq_;
    int callbacks_per_cq_;
    DebugService::AsyncService service_;
    std::unique_ptr<Server> server_;
    std::vector<std::unique_ptr<ServerCompletionQueue>> cqs_;
    std::vector<std::thread> threads_;
};

void PrintUsage(const char* program_name) {
    std::cout << "Usage: " << program_name << " [options]" << std::endl;
    std::cout << "Options:" << std::endl;
    std::cout << "  -h, --help                Show this help message" << std::endl;
    std::cout << "  --host <hostname>         Server hostname (default: localhost)" << std::endl;
    std::cout << "  --port <port>             Server port (default: 2137)" << std::endl;
    std::cout << "  --num-cqs <N>             Number of completion queues (default: 1)" << std::endl;
    std::cout << "  --workers-per-cq <N>      Number of worker threads per completion queue (default: 1)" << std::endl;
    std::cout << "  --callbacks-per-cq <N>    Number of callbacks per completion queue (default: 100)" << std::endl;
}

int main(int argc, char** argv) {
    std::string host = "localhost";
    int port = 2137;
    int num_cqs = 1;
    int workers_per_cq = 1;
    int callbacks_per_cq = 100;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "-h" || arg == "--help") {
            PrintUsage(argv[0]);
            return 0;
        } else if (arg == "--host" && i + 1 < argc) {
            host = argv[++i];
        } else if (arg == "--port" && i + 1 < argc) {
            port = std::stoi(argv[++i]);
        } else if (arg == "--num-cqs" && i + 1 < argc) {
            num_cqs = std::stoi(argv[++i]);
        } else if (arg == "--workers-per-cq" && i + 1 < argc) {
            workers_per_cq = std::stoi(argv[++i]);
        } else if (arg == "--callbacks-per-cq" && i + 1 < argc) {
            callbacks_per_cq = std::stoi(argv[++i]);
        } else {
            std::cerr << "Unknown option: " << arg << std::endl;
            PrintUsage(argv[0]);
            return 1;
        }
    }

    std::string server_address = host + ":" + std::to_string(port);
    ServerImpl server(server_address, num_cqs, workers_per_cq, callbacks_per_cq);
    server.Run();

    return 0;
} 