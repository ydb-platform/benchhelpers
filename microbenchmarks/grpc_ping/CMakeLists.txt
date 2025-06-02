cmake_minimum_required(VERSION 3.13)
project(grpc_ping CXX)

add_compile_options(-D_GLIBCXX_USE_CXX11_ABI=0)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -O3 -DNDEBUG -fno-omit-frame-pointer")
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} -g -fno-omit-frame-pointer")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

# 1. fetch GRPC (and protobuf as well)

set(gRPC_BUILD_TESTS OFF)
set(gRPC_BUILD_GRPC_CPP_PLUGIN ON)
set(protobuf_INSTALL OFF CACHE INTERNAL "")

set(gRPC_SSL_PROVIDER "package")
set(gRPC_CARES_PROVIDER "package")
set(gRPC_ABSL_PROVIDER "module")
set(gRPC_RE2_PROVIDER "package")
set(gRPC_ZLIB_PROVIDER "package")

FetchContent_Declare(
    gRPC
    GIT_REPOSITORY https://github.com/grpc/grpc
    GIT_TAG v1.54.1
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(gRPC)

add_library(gRPC::grpc++ ALIAS grpc++)
add_executable(gRPC::grpc_cpp_plugin ALIAS grpc_cpp_plugin)

# 2. generate proto stuff

set(PROTOS_PATH ${CMAKE_CURRENT_SOURCE_DIR}/proto)
set(PROTO_FILES ${PROTOS_PATH}/debug.proto)
set(PROTO_BINARY_DIR ${CMAKE_CURRENT_BINARY_DIR}/proto)

file(MAKE_DIRECTORY ${PROTO_BINARY_DIR})

add_custom_command(
    OUTPUT
        ${PROTO_BINARY_DIR}/debug.pb.cc
        ${PROTO_BINARY_DIR}/debug.pb.h
        ${PROTO_BINARY_DIR}/debug.grpc.pb.cc
        ${PROTO_BINARY_DIR}/debug.grpc.pb.h
    COMMAND protobuf::protoc
    ARGS
        --proto_path=${PROTOS_PATH}
        --cpp_out=${PROTO_BINARY_DIR}
        --grpc_out=${PROTO_BINARY_DIR}
        --plugin=protoc-gen-grpc=$<TARGET_FILE:gRPC::grpc_cpp_plugin>
        ${PROTO_FILES}
    DEPENDS
        ${PROTO_FILES}
        protobuf::protoc
        gRPC::grpc_cpp_plugin
    VERBATIM
)

# generate a dummy target to trigger generation
add_custom_target(proto-generated
    DEPENDS
        ${PROTO_BINARY_DIR}/debug.pb.cc
        ${PROTO_BINARY_DIR}/debug.pb.h
        ${PROTO_BINARY_DIR}/debug.grpc.pb.cc
        ${PROTO_BINARY_DIR}/debug.grpc.pb.h
)

# create proto object library
add_library(proto-objects OBJECT
    ${PROTO_BINARY_DIR}/debug.pb.cc
    ${PROTO_BINARY_DIR}/debug.grpc.pb.cc
)

add_dependencies(proto-objects proto-generated)

target_include_directories(proto-objects PUBLIC
    ${PROTO_BINARY_DIR}
)

target_link_libraries(proto-objects PUBLIC
    protobuf::libprotobuf
    gRPC::grpc++
)

# 3. grpc_ping_client

add_executable(grpc_ping_client
    grpc_ping_client.cpp
)

add_dependencies(grpc_ping_client proto-generated)

target_link_libraries(grpc_ping_client PRIVATE
    proto-objects
    protobuf::libprotobuf
    gRPC::grpc++
    grpc
    pthread
    dl
)

target_include_directories(grpc_ping_client PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${PROTO_BINARY_DIR}
    ${_deps/grpc-src_SOURCE_DIR}/src/core/lib/iomgr
)

# 4. grpc_ping_server

add_executable(grpc_ping_server
    grpc_ping_server.cpp
)

add_dependencies(grpc_ping_server proto-generated)

target_link_libraries(grpc_ping_server PRIVATE
    proto-objects
    protobuf::libprotobuf
    gRPC::grpc++
    grpc
    pthread
    dl
)

target_include_directories(grpc_ping_server PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${PROTO_BINARY_DIR}
    ${_deps/grpc-src_SOURCE_DIR}/src/core/lib/iomgr
)
