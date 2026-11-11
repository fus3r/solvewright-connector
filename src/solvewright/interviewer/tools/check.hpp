// check.hpp: the test macros of Solvewright's C++ rounds.
//
//   TEST(name) { ... }            one test; each runs in a fresh process of its own
//   CHECK(condition)              the test fails here when the condition is false
//   CHECK_EQ(actual, expected)    the test fails here when the two differ, printing both
//
// A test stops at its first failed check. To print a type of your own in a failed
// CHECK_EQ, declare `void show_value(std::string& out, const YourType& value)` beside it.
// Results are printed one per line, each starting with "@@sw ".
#pragma once

#include <concepts>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iterator>
#include <optional>
#include <string>
#include <string_view>
#include <type_traits>
#include <utility>

namespace sw_check {

struct Test { const char* name; void (*run)(); const char* file; int line; };
inline Test tests[512];
inline int count = 0;
struct Add {
  Add(const char* name, void (*run)(), const char* file, int line) {
    if (count < 512) tests[count++] = Test{name, run, file, line};
  }
};

inline void quoted(std::string& out, std::string_view value) {
  out += '"';
  for (char c : value) {
    if (c == '\n') out += "\\n";
    else if (c == '"') out += "\\\"";
    else if (c == '\\') out += "\\\\";
    else out += c;
  }
  out += '"';
}

template <class T> concept StringLike = std::convertible_to<const T&, std::string_view>;
template <class T> concept Range = requires(const T& range) { std::begin(range); std::end(range); };
template <class T> concept PairLike = requires(const T& pair) { pair.first; pair.second; };
template <class T> concept OptionalLike = requires(const T& value) { value.has_value(); *value; };

template <class T> void show(std::string& out, const T& value) {
  char buffer[64];
  if constexpr (std::is_same_v<T, bool>) out += value ? "true" : "false";
  else if constexpr (std::is_same_v<T, char>) { out += '\''; out += value; out += '\''; }
  else if constexpr (std::is_same_v<T, std::nullopt_t>) out += "nullopt";
  else if constexpr (std::is_same_v<T, std::nullptr_t>) out += "nullptr";
  else if constexpr (requires { show_value(out, value); }) show_value(out, value);
  else if constexpr (std::is_enum_v<T>) show(out, static_cast<std::underlying_type_t<T>>(value));
  else if constexpr (std::is_integral_v<T> && std::is_signed_v<T>) { std::snprintf(buffer, sizeof buffer, "%lld", static_cast<long long>(value)); out += buffer; }
  else if constexpr (std::is_integral_v<T>) { std::snprintf(buffer, sizeof buffer, "%llu", static_cast<unsigned long long>(value)); out += buffer; }
  else if constexpr (std::is_floating_point_v<T>) { std::snprintf(buffer, sizeof buffer, "%.17g", static_cast<double>(value)); out += buffer; }
  else if constexpr (StringLike<T>) quoted(out, std::string_view(value));
  else if constexpr (OptionalLike<T>) { if (value.has_value()) show(out, *value); else out += "nullopt"; }
  else if constexpr (PairLike<T>) { out += '{'; show(out, value.first); out += ", "; show(out, value.second); out += '}'; }
  else if constexpr (Range<T>) {
    out += '{';
    int shown = 0;
    for (const auto& item : value) {
      if (shown) out += ", ";
      if (++shown > 24) { out += "..."; break; }
      show(out, item);
    }
    out += '}';
  }
  else if constexpr (std::is_pointer_v<T>) { std::snprintf(buffer, sizeof buffer, "%p", static_cast<const void*>(value)); out += buffer; }
  else out += "(a value check.hpp cannot print)";
}

[[noreturn]] inline void fail(const char* file, int line, const std::string& message) {
  std::fflush(stdout);
  std::printf("\n@@sw fail %s:%d: %s\n", file, line, message.c_str());
  std::fflush(stdout);
  std::_Exit(1);
}

inline void check(bool ok, const char* file, int line, const char* condition) {
  if (!ok) fail(file, line, std::string("CHECK(") + condition + ") failed");
}

template <class A, class B> void check_eq(const char* file, int line, const char* arguments, const A& actual, const B& expected) {
  if (actual == expected) return;
  std::string message = std::string("CHECK_EQ(") + arguments + ") failed: got ";
  show(message, actual);
  message += ", expected ";
  show(message, expected);
  fail(file, line, message);
}

inline int run(int argc, char** argv) {
  if (argc < 2) { std::printf("@@sw usage: a test name, or --list\n"); return 2; }
  if (std::strcmp(argv[1], "--list") == 0) {
    for (int i = 0; i < count; ++i) std::printf("@@sw test %s %s:%d\n", tests[i].name, tests[i].file, tests[i].line);
    return 0;
  }
  for (int i = 0; i < count; ++i) {
    if (std::strcmp(argv[1], tests[i].name) == 0) {
      tests[i].run();
      std::fflush(stdout);
      std::printf("\n@@sw pass\n");
      return 0;
    }
  }
  std::printf("@@sw missing %s\n", argv[1]);
  return 3;
}

}  // namespace sw_check

#define TEST(name)                                                                   \
  static void sw_test_##name();                                                      \
  static ::sw_check::Add sw_add_##name(#name, &sw_test_##name, __FILE__, __LINE__);  \
  static void sw_test_##name()
#define CHECK(...) ::sw_check::check(static_cast<bool>(__VA_ARGS__), __FILE__, __LINE__, #__VA_ARGS__)
#define CHECK_EQ(...) ::sw_check::check_eq(__FILE__, __LINE__, #__VA_ARGS__, __VA_ARGS__)

int main(int argc, char** argv) { return ::sw_check::run(argc, argv); }
